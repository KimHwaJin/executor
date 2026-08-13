# External Test Harnesses

This directory contains systems that are not part of the Executor service but are used to exercise
its public integration contracts locally. Executor production code remains under `src/`; harnesses
must communicate through MCP, REST, Redis Streams, or Runtime APIs and must not import Executor
internals or access Executor-owned database tables.

## Components

- [`jupyter/`](jupyter/README.md): Jupyter image and native runner, `basic` and `ml` kernels,
  Executor-compatible Jupyter server extension, and the ignored shared workspace used by local
  Runtime Targets.
- [`agent/`](agent/README.md): isolated LangGraph/LangChain Agent Server project used to exercise
  Executor's public interfaces.

PostgreSQL and Redis remain in the repository-root Compose file because they are required Executor
infrastructure, not simulated external clients. Compose is the orchestration entry point and builds
the Jupyter harness from this directory.

Generated environments and runtime data stay inside their owning harness directories and are
ignored by Git:

```text
test_harness/
├── agent/.venv/
└── jupyter/
    ├── .native/
    ├── extension/.venv/
    └── workspace/
```
