"""Workflow orchestration: the persisted queue, the engine and the worker.

Importing this package registers every task handler — the deterministic steps and
the agent steps alike.

That is done here rather than in the API's lifespan because whether a handler
exists is a property of the code being installed, not of whether a particular
process happens to run a worker thread. An API process paired with an external
worker, a CLI drain, or a test that drives the queue directly would otherwise see
every task kind as unhandled and block the entire run.

The agent import is last: ``app.agents.review.handlers`` imports from
``app.orchestration.handlers``, so the submodule must already be importable when
it runs.
"""

from __future__ import annotations

from app.agents import review as _review  # noqa: F401  (registers agent handlers)
from app.orchestration import steps as _steps  # noqa: F401  (registers handlers)
