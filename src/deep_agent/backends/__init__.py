"""Deepagents backend adapters.

The MongoDB-backed backend plugs our :class:`VirtualFilesystem` into
deepagents' ``BackendProtocol`` so the agent's built-in filesystem tools
persist blobs to S3 with metadata in MongoDB instead of LangGraph state.
"""
from .mongo_backend import MongoVfsBackend, mongo_backend_factory

__all__ = ["MongoVfsBackend", "mongo_backend_factory"]
