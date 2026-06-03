"""fetch_and_cache tool.

A layered SSRF + body-cap sits on top of the fetch tool. The tests
mock httpx.Client.stream() because the real code now streams bodies
to enforce ``Settings.fetch_max_bytes``.
"""
from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    from deep_agent import config

    config.get_settings.cache_clear()


def _stub_stream_client(body: bytes, *, status: int = 200, raise_on_get=None):  # type: ignore[no-untyped-def]
    """Return a mock httpx.Client whose .stream() yields ``body`` once."""
    resp = MagicMock()
    resp.iter_bytes = lambda: iter([body])
    resp.raise_for_status = MagicMock()
    if status >= 400:
        import httpx

        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]
        )

    stream_cm = MagicMock()
    stream_cm.__enter__ = MagicMock(return_value=resp)
    stream_cm.__exit__ = MagicMock(return_value=None)

    client = MagicMock()
    if raise_on_get is not None:
        client.stream = MagicMock(side_effect=raise_on_get)
    else:
        client.stream = MagicMock(return_value=stream_cm)
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=None)
    return client


class _FakeVS:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[dict[str, Any]]]] = []

    def add_texts(
        self, texts: list[str], metadatas: list[dict[str, Any]] | None = None
    ) -> list[str]:
        self.calls.append((list(texts), list(metadatas or [])))
        return ["id"] * len(texts)


class _FakeKBColl:
    def __init__(self, existing_hashes: set[str]) -> None:
        self._existing = existing_hashes

    def count_documents(self, query: dict[str, Any], limit: int | None = None) -> int:
        h = query.get("metadata.content_hash")
        return 1 if h in self._existing else 0


class _StubVfs:
    def __init__(self) -> None:
        self.written: list[tuple[str, str, bytes]] = []

    def write_file(
        self, thread_id: str, path: str, data: bytes, *, content_type: str
    ) -> MagicMock:
        self.written.append((thread_id, path, data))
        m = MagicMock()
        m.locator = "loc-x"
        return m


def _patch_safe_url(ok: bool = True, reason: str = ""):  # type: ignore[no-untyped-def]
    return patch(
        "deep_agent.tools.fetch_and_cache._is_safe_url",
        return_value=(ok, reason),
    )


def test_TC_09_020_fetch_and_cache_writes_vfs_and_kb() -> None:
    from deep_agent.tools import fetch_and_cache as fc

    vs = _FakeVS()
    vfs = _StubVfs()
    body = b"a" * 2500
    kb_coll = _FakeKBColl(existing_hashes=set())

    client = _stub_stream_client(body)
    with patch("deep_agent.tools.fetch_and_cache.httpx") as httpx_mod, patch(
        "deep_agent.tools.fetch_and_cache.build_vector_store", return_value=vs
    ), patch("deep_agent.tools.fetch_and_cache.get_vfs", return_value=vfs), patch(
        "deep_agent.tools.fetch_and_cache.get_db"
    ) as gdb, _patch_safe_url(ok=True):
        httpx_mod.Client.return_value = client
        httpx_mod.HTTPError = Exception
        gdb.return_value.__getitem__.return_value = kb_coll

        result = fc.fetch_and_cache.invoke(
            {"url": "https://example.com/x", "thread_id": "t1"}
        )

    assert result["cached"] is False
    assert result["chunks_added"] >= 2
    expected_hash = hashlib.sha256(body).hexdigest()
    assert len(vfs.written) == 1
    _, path, _ = vfs.written[0]
    assert path == f"web_cache/{expected_hash[:12]}.html"
    assert len(vs.calls) == 1
    _, metadatas = vs.calls[0]
    assert metadatas[0]["source"] == "https://example.com/x"
    assert metadatas[0]["content_hash"] == expected_hash


def test_TC_09_021_fetch_without_thread_id_skips_vfs() -> None:
    from deep_agent.tools import fetch_and_cache as fc

    vs = _FakeVS()
    vfs = _StubVfs()
    body = b"short body"
    kb_coll = _FakeKBColl(existing_hashes=set())

    client = _stub_stream_client(body)
    with patch("deep_agent.tools.fetch_and_cache.httpx") as httpx_mod, patch(
        "deep_agent.tools.fetch_and_cache.build_vector_store", return_value=vs
    ), patch("deep_agent.tools.fetch_and_cache.get_vfs", return_value=vfs), patch(
        "deep_agent.tools.fetch_and_cache.get_db"
    ) as gdb, _patch_safe_url(ok=True):
        httpx_mod.Client.return_value = client
        httpx_mod.HTTPError = Exception
        gdb.return_value.__getitem__.return_value = kb_coll

        result = fc.fetch_and_cache.invoke({"url": "https://example.com/x"})

    assert result["cached"] is False
    assert vfs.written == []


def test_TC_09_030_fetch_hash_dedupe() -> None:
    from deep_agent.tools import fetch_and_cache as fc

    vs = _FakeVS()
    vfs = _StubVfs()
    body = b"already-seen-body"
    expected_hash = hashlib.sha256(body).hexdigest()
    kb_coll = _FakeKBColl(existing_hashes={expected_hash})

    client = _stub_stream_client(body)
    with patch("deep_agent.tools.fetch_and_cache.httpx") as httpx_mod, patch(
        "deep_agent.tools.fetch_and_cache.build_vector_store", return_value=vs
    ), patch("deep_agent.tools.fetch_and_cache.get_vfs", return_value=vfs), patch(
        "deep_agent.tools.fetch_and_cache.get_db"
    ) as gdb, _patch_safe_url(ok=True):
        httpx_mod.Client.return_value = client
        httpx_mod.HTTPError = Exception
        gdb.return_value.__getitem__.return_value = kb_coll

        result = fc.fetch_and_cache.invoke(
            {"url": "https://example.com/seen", "thread_id": "t1"}
        )

    assert result == {
        "url": "https://example.com/seen",
        "cached": True,
        "chunks_added": 0,
    }
    assert vs.calls == []
    assert vfs.written == []


def test_TC_09_040_fetch_returns_structured_error_on_http_failure() -> None:
    """HTTP / SSL / network errors return a structured ``error`` field."""
    import httpx

    from deep_agent.tools import fetch_and_cache as fc

    vs = _FakeVS()
    vfs = _StubVfs()
    kb_coll = _FakeKBColl(existing_hashes=set())

    client = _stub_stream_client(
        b"", raise_on_get=httpx.ConnectError("nope")
    )
    with patch("deep_agent.tools.fetch_and_cache.build_vector_store", return_value=vs), patch(
        "deep_agent.tools.fetch_and_cache.get_vfs", return_value=vfs
    ), patch("deep_agent.tools.fetch_and_cache.get_db") as gdb, patch(
        "deep_agent.tools.fetch_and_cache.httpx"
    ) as httpx_mod, _patch_safe_url(ok=True):
        httpx_mod.Client.return_value = client
        httpx_mod.HTTPError = httpx.HTTPError
        gdb.return_value.__getitem__.return_value = kb_coll

        result = fc.fetch_and_cache.invoke({"url": "https://example.com/broken"})

    assert result["url"] == "https://example.com/broken"
    assert "error" in result
    assert "ConnectError" in result["error"]
    assert vs.calls == []


# --- SSRF + body-cap ---------------------------------


def test_TC_R501_100_scheme_rejected() -> None:
    """Non-http(s) schemes refused before any I/O."""
    from deep_agent.tools import fetch_and_cache as fc

    result = fc.fetch_and_cache.invoke({"url": "file:///etc/passwd"})
    assert "error" in result
    assert "REFUSED" in result["error"]
    assert "file" in result["error"]


def test_TC_R501_101_localhost_rejected() -> None:
    """Loopback IP refused."""
    from deep_agent.tools import fetch_and_cache as fc

    result = fc.fetch_and_cache.invoke({"url": "http://127.0.0.1/"})
    assert "REFUSED" in result["error"]


def test_TC_R501_101_rfc1918_rejected() -> None:
    """RFC 1918 (private) refused."""
    from deep_agent.tools import fetch_and_cache as fc

    result = fc.fetch_and_cache.invoke({"url": "http://192.168.1.1/"})
    assert "REFUSED" in result["error"]


def test_TC_R501_101_link_local_rejected() -> None:
    """Link-local (cloud metadata!) refused."""
    from deep_agent.tools import fetch_and_cache as fc

    result = fc.fetch_and_cache.invoke({"url": "http://169.254.169.254/latest/meta-data/"})
    assert "REFUSED" in result["error"]


def test_TC_R501_101_ipv6_loopback_rejected() -> None:
    """IPv6 ::1 loopback refused."""
    from deep_agent.tools import fetch_and_cache as fc

    result = fc.fetch_and_cache.invoke({"url": "http://[::1]/"})
    assert "REFUSED" in result["error"]


def test_TC_R501_101_dns_rebind_protection() -> None:
    """DNS that resolves to a private IP for a public-looking host is refused."""
    from deep_agent.tools import fetch_and_cache as fc

    with patch("deep_agent.tools.fetch_and_cache.socket.getaddrinfo") as gai:
        # Pretend example.com resolves to 10.0.0.5
        gai.return_value = [
            (None, None, None, None, ("10.0.0.5", 0)),
        ]
        result = fc.fetch_and_cache.invoke({"url": "https://example.com/"})
    assert "REFUSED" in result["error"]
    assert "10.0.0.5" in result["error"]


def test_TC_R501_102_body_cap_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Body exceeding FETCH_MAX_BYTES is refused mid-stream."""
    from deep_agent.tools import fetch_and_cache as fc

    monkeypatch.setenv("FETCH_MAX_BYTES", "1024")
    from deep_agent import config

    config.get_settings.cache_clear()

    big_body = b"x" * 4096

    client = _stub_stream_client(big_body)
    with patch("deep_agent.tools.fetch_and_cache.httpx") as httpx_mod, patch(
        "deep_agent.tools.fetch_and_cache.build_vector_store", return_value=_FakeVS()
    ), patch(
        "deep_agent.tools.fetch_and_cache.get_vfs", return_value=_StubVfs()
    ), patch("deep_agent.tools.fetch_and_cache.get_db"), _patch_safe_url(ok=True):
        httpx_mod.Client.return_value = client
        httpx_mod.HTTPError = Exception
        result = fc.fetch_and_cache.invoke({"url": "https://example.com/big"})

    assert "REFUSED" in result["error"]
    assert "1024" in result["error"]


def test_TC_R501_103_zero_disables_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """FETCH_MAX_BYTES=0 disables the cap."""
    from deep_agent.tools import fetch_and_cache as fc

    monkeypatch.setenv("FETCH_MAX_BYTES", "0")
    from deep_agent import config

    config.get_settings.cache_clear()

    body = b"x" * (5 * 1024 * 1024)  # 5 MiB; would exceed default 2 MiB
    kb_coll = _FakeKBColl(existing_hashes=set())

    client = _stub_stream_client(body)
    with patch("deep_agent.tools.fetch_and_cache.httpx") as httpx_mod, patch(
        "deep_agent.tools.fetch_and_cache.build_vector_store", return_value=_FakeVS()
    ), patch(
        "deep_agent.tools.fetch_and_cache.get_vfs", return_value=_StubVfs()
    ), patch("deep_agent.tools.fetch_and_cache.get_db") as gdb, _patch_safe_url(ok=True):
        httpx_mod.Client.return_value = client
        httpx_mod.HTTPError = Exception
        gdb.return_value.__getitem__.return_value = kb_coll
        result = fc.fetch_and_cache.invoke({"url": "https://example.com/huge"})

    assert "error" not in result or "REFUSED" not in (result.get("error") or "")
    assert result.get("cached") is False
