# Documentation

Reference documentation for `mongodb-langchain-deep-agents` — the engine behind **Agent Cartsmith**, a grocery / recipe / savings concierge.

## Start here

- **[Getting Started](getting-started.md)** - install, configure, and run your first retail shopping-assistant turn
- **[Architecture](architecture.md)** - main planner + 6 retail subagents, VFS, agent_log persistence (AgentLogMiddleware)

## Reference

- **[API Reference](api-reference.md)** - HTTP endpoints, CLI commands, Python factories, tools
- **[Configuration](configuration.md)** - every environment variable, collection, and index name

## How-to guides

- **[Deploy](DEPLOY.md)** - one-command Docker stack via `scripts/deploy.sh`, plus container-platform notes
- **[Developer Guide](developer-guide.md)** - local development loop: lint, type-check, test
- **[Testing](testing.md)** - unit vs. integration tests
- **[Functional Testing](functional-testing.md)** - reproducible end-to-end runbook against live Atlas
- **[Verification](verification.md)** - end-to-end verification runbook for the Agent Cartsmith retail assistant
- **[Deployment](deployment.md)** - Dockerfile, docker-compose, LangGraph Platform (lower-level)
- **[Streaming](streaming.md)** - Kafka → Atlas Stream Processing → change-stream ingestion
- **[Evals](evals.md)** - starter LangSmith dataset + uploader + custom evaluators

## Explanation

- **[Security Model](security.md)** - TLS enforcement, safety wrapper, memory + VFS scoping, RBAC
- **[VFS Backends](vfs-backends.md)** - S3 backend, IAM policy, contract tests
- **[MongoDB Backend](mongodb-backend.md)** - how deepagents' filesystem tools route through MongoDB

## Operator runbooks

- **[operators/atlas-cli-setup.md](operators/atlas-cli-setup.md)** - one-shot cluster + index provisioning
- **[operators/rbac-example.md](operators/rbac-example.md)** - least-privilege Atlas roles
