# syntax=docker/dockerfile:1.7
#
# Single-repo build: context is this repository's root. The
# ``langchain-mongodb-agent-log`` dependency is pulled from its published
# Git tag (see pyproject.toml), so no sibling checkout is needed.

# --- Stage 1: build ---------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS build

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# ``git`` is required to resolve the git+https direct reference for
# langchain-mongodb-agent-log; the uv slim base image does not ship it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Stage 2: runtime -------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 1000 deep_agent
WORKDIR /app

COPY --from=build --chown=deep_agent:deep_agent /app/.venv /app/.venv
COPY --chown=deep_agent:deep_agent src ./src
COPY --chown=deep_agent:deep_agent examples ./examples
# SKILL.md corpus loaded by deepagents at graph-build time.
# Settings.agent_skills_dir defaults to /app/AgentSkills.
COPY --chown=deep_agent:deep_agent AgentSkills ./AgentSkills
COPY --chown=deep_agent:deep_agent pyproject.toml README.md ./

USER deep_agent

EXPOSE 8000

CMD ["deep-agent", "serve", "--host", "0.0.0.0", "--port", "8000"]
