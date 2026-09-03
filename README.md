# Incident Copilot

A Slack-native incident response copilot. An alert lands in a channel; the bot investigates —
gathering metrics, logs, recent changes, runbooks, and *similar past incidents* — then proposes
concrete remediation actions with its reasoning. An engineer approves in the thread. The bot
executes the approved actions, re-reads the metrics, and reports whether the incident actually
recovered.

> **Status: under construction.** Foundation, control plane, job queue, and the Claude agent
> are complete; the Slack layer is next. See [`PLAN.md`](PLAN.md) for the architecture and
> remaining phases.

## Why it's built this way

- **The agent's tools are read-only.** Investigation structurally cannot mutate anything. Every
  change goes through an explicit human approval gate with a single-use, server-side token.
- **Metrics are derived from control-plane state**, not read from fixtures. Approving the right
  fix visibly drops the error rate; approving the wrong one doesn't, and verification fails
  honestly.
- **Slack work happens in a durable queue.** Handlers ack within Slack's 3-second budget and
  enqueue; a separate worker claims jobs from Postgres with `SELECT … FOR UPDATE SKIP LOCKED`,
  retries with backoff, and survives restarts.
- **One transport abstraction, two adapters.** Socket Mode locally (no tunnel), Bolt's ASGI
  adapter on FastAPI in production (stateless, horizontally scalable). Same handler code.

## Quick start

```bash
make install     # venv + dependencies
make db-up       # Postgres 16 + pgvector via Docker
make migrate     # apply migrations
make index       # build the runbook + incident-history search index
make check       # lint, type-check, test
make api         # http://localhost:8000/healthz
make worker      # the job worker, in a second terminal
```

Copy `.env.example` to `.env` and fill in credentials as you need them. The test suite needs no
credentials at all — it fakes the model's *choices* while running the real tools against the real
database. To exercise the live model, set `ANTHROPIC_API_KEY` and run `make test-live`.

## Layout

| Path | Contents |
|---|---|
| `incident_copilot/config.py` | Typed settings — the only place env vars are read |
| `incident_copilot/db/` | SQLAlchemy models, session management, Alembic migrations |
| `incident_copilot/controlplane/` | Simulated ops environment; metrics derived from state |
| `incident_copilot/jobs/` | Postgres job queue and worker |
| `incident_copilot/agent/` | Claude tool-use loop and structured proposals |
| `incident_copilot/retrieval/` | Corpus indexing and Postgres full-text search |
| `incident_copilot/slack/` | Bolt app, Block Kit, approval tokens *(phase 5)* |
| `scenarios/` | Five incident scenario packs |
| `seed/kb/`, `seed/history/` | Runbooks, policies, and 24 resolved past incidents |

## Commands

Run `make` with no arguments for the full list.
