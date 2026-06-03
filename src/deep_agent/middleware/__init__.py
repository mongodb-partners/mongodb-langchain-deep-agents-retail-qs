"""Custom deepagents middleware kept in this repo.

The previous ``CheckpointMirrorMiddleware`` has been extracted
into the standalone ``langchain-mongodb-agent-log`` package and is now
imported from there at graph-build time (see
``deep_agent.graph._middleware_chain``).

What remains here:

- :class:`PatchDanglingToolCallsMiddleware` repairs dangling
  ``tool_use``/``tool_result`` pairings that Bedrock's strict validator
  rejects; registered conditionally on Bedrock providers only.
"""
from .patch_dangling import PatchDanglingToolCallsMiddleware

__all__ = ["PatchDanglingToolCallsMiddleware"]
