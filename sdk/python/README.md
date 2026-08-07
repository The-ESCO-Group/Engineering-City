# ESCO City external-agent SDK bridge

The gateway accepts externally hosted OpenAI Agents SDK and Claude SDK workers
without giving either provider database, filesystem, approval, waiver, or
release authority.

1. Get a user access token by signing in through `POST /api/login`, then set
   `ESCO_ACCESS_TOKEN`, `ESCO_PROJECT_ID`, `ESCO_PROVIDER`, and
   `ESCO_AGENT_MISSION` in the shell that will deploy the worker.
2. Run `python deploy_agent.py`. Copy its returned `agent_token` into
   `ESCO_AGENT_TOKEN`; treat it like a password and do not log or commit it.
3. Install one provider SDK in a separate worker environment:
   `pip install openai-agents pydantic` or `pip install anthropic`.
4. Set `ESCO_AGENT_TOKEN` and the provider API key. For Claude, also set the
   exact model available to your Anthropic account in `ANTHROPIC_MODEL`.
5. Run `python openai_worker.py` or `python claude_worker.py`.

The worker reads bounded project context, emits live semantic location/status
updates, and submits one idempotent result. ESCO re-validates every action and
retains all rejected attempts. Technical claims require real evidence anchor
IDs, while human approval and release decisions remain unreachable.

Each deployment is one mission. A worker may publish status and location while
it works, but after its first result is submitted it cannot resume or replace
that result. Exact submission retries are safe and return the original run.
