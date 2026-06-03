"""Fetch a URL, cache the raw page in the VFS, and ingest chunks into the KB.

Dedupe via SHA-256 of the response body: when the same body has already been
ingested (``metadata.content_hash`` in ``knowledge_base``), the tool returns
``{cached: True, chunks_added: 0}`` without re-embedding.

SSRF guard + body-size cap. The tool refuses:
- non-http(s) schemes (``file://``, ``ftp://``, ``data:``, etc.)
- private / loopback / link-local / multicast / reserved IPs after DNS
- response bodies that exceed ``Settings.fetch_max_bytes`` while streaming.

The DNS lookup happens *before* the GET so an attacker cannot bind a
public hostname to a private IP (DNS rebinding / 169.254.169.254 cloud
metadata / RFC 1918) and slip past the guard.
"""
from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langchain_text_splitters import RecursiveCharacterTextSplitter

from ..config import get_settings
from ..persistence.mongo import get_db
from ..persistence.vector_store import build_vector_store
from ..vfs import get_vfs

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0
_USER_AGENT = "deep-agent/0.1 (+https://www.mongodb.com/)"
# RFC 6598 carrier-grade NAT / shared address space. ipaddress reports this as
# global, so it must be refused explicitly in the SSRF guard.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Return ``(ok, reason)``. Refuse non-http(s) schemes, missing host,
    and any host whose resolved IPs include a private/reserved range.

    NOTE on TOCTOU: this is a best-effort guard. The DNS resolution we do
    here is not the resolution httpx will do on the actual request — a
    DNS rebinding attack could return a public IP at check-time and a
    private IP at fetch-time. The current threat model assumes the
    LLM-supplied URL is untrusted but DNS itself is not actively
    adversarial; tightening this further would require wiring an httpx
    transport that pins the resolved IP from this check through the
    request. Defer until that threat surfaces.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"refusing scheme {parsed.scheme!r}"
    host = parsed.hostname
    if not host:
        return False, "missing host"
    try:
        addrs = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        return False, f"DNS resolution failed: {exc}"
    for _, _, _, _, sockaddr in addrs:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            return False, f"could not parse address for host {host!r}"
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
            # CGNAT / shared address space (RFC 6598). Python reports this
            # is_global=True / is_private=False, so it slips past
            # the checks above — but Tailscale and carrier-grade NAT use it.
            or ip in _CGNAT_V4
        ):
            return False, f"refusing non-public IP {ip}"
    return True, ""


def _already_ingested(content_hash: str) -> bool:
    s = get_settings()
    kb = get_db()[s.knowledge_base_collection]
    return bool(kb.count_documents({"metadata.content_hash": content_hash}, limit=1))


@tool
def fetch_and_cache(
    url: str,
    thread_id: str | None = None,
    config: RunnableConfig | None = None,
) -> dict[str, Any]:
    """Fetch ``url``, store the raw page in the VFS, and ingest chunks into the KB.

    When ``thread_id`` is provided the raw body is cached under
    ``web_cache/<hash[:12]>.html`` in the VFS. If the content hash is already
    present in ``knowledge_base``, the call is a no-op and returns
    ``{cached: True, chunks_added: 0}``.

    HTTP/SSL/network errors return ``{"url": url, "error": "<message>"}``
    rather than raising. Tool-result blocks must always land next to their
    tool-use blocks for Bedrock's strict validator, so any unhandled exception
    here leaves the turn in a state the LLM cannot recover from.

    A pre-flight SSRF guard rejects non-http(s) schemes and any host whose
    resolved IPs are private/reserved. Body is streamed with a
    cap of ``Settings.fetch_max_bytes`` (default 2 MiB; 0 disables).
    """
    ok, reason = _is_safe_url(url)
    if not ok:
        log.warning("fetch_and_cache REFUSED for %s: %s", url, reason)
        return {"url": url, "error": f"REFUSED: {reason}"}

    s = get_settings()
    cap = s.fetch_max_bytes
    body_bytes: list[bytes] = []
    total = 0
    try:
        # Pin follow_redirects=False explicitly. _is_safe_url only validates
        # the ORIGINAL url; following a redirect to an internal
        # host would re-open SSRF. Don't rely on httpx's implicit default.
        with httpx.Client(
            timeout=_DEFAULT_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
            follow_redirects=False,
        ) as client, client.stream("GET", url) as resp:
            resp.raise_for_status()
            for piece in resp.iter_bytes():
                total += len(piece)
                if cap > 0 and total > cap:
                    return {
                        "url": url,
                        "error": f"REFUSED: body exceeds {cap} bytes",
                    }
                body_bytes.append(piece)
    except (httpx.HTTPError, OSError) as exc:
        log.warning("fetch_and_cache HTTP/network failure for %s: %s", url, exc)
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"[:400]}

    body = b"".join(body_bytes).decode("utf-8", errors="replace")
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()

    if _already_ingested(content_hash):
        return {"url": url, "cached": True, "chunks_added": 0}

    if thread_id:
        # Best-effort VFS cache — ingestion must not fail because the VFS is misbehaving.
        with contextlib.suppress(Exception):
            vfs = get_vfs()
            vfs.write_file(
                thread_id,
                f"web_cache/{content_hash[:12]}.html",
                body.encode("utf-8"),
                content_type="text/html",
            )

    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    chunks = splitter.split_text(body)
    metadatas = [{"source": url, "content_hash": content_hash} for _ in chunks]
    try:
        vector_store = build_vector_store()
        vector_store.add_texts(chunks, metadatas=metadatas)
    except Exception as exc:
        log.warning("fetch_and_cache KB ingest failed for %s: %s", url, exc)
        return {"url": url, "error": f"KB ingest failed: {type(exc).__name__}"}

    log.info("fetch_and_cache ingested %d chunks from %s", len(chunks), url)
    return {"url": url, "cached": False, "chunks_added": len(chunks)}
