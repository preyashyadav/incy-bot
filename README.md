# Incident Copilot

A Slack-native incident response copilot. An alert lands in a channel; the bot investigates —
gathering metrics, logs, recent changes, runbooks, and *similar past incidents* — then proposes
concrete remediation actions with its reasoning. An engineer approves in the thread. The bot
executes the approved actions, re-reads the metrics, and reports whether the incident actually
recovered.

> **Status: under construction.** Everything up to human approval works end to end: alert →
> investigate → proposal card → approve/reject. Executing the approved remediation and
> verifying recovery is the next phase. See [`PLAN.md`](PLAN.md).

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
make slack       # Socket Mode, in a third (needs SLACK_APP_TOKEN)
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
| `incident_copilot/slack/` | Bolt app (both transports), Block Kit, approval flow |
| `scenarios/` | Five incident scenario packs |
| `seed/kb/`, `seed/history/` | Runbooks, policies, and 24 resolved past incidents |

## Using it from Slack

```
/incident list                          list the scenarios
/incident triage payments_gateway_timeout   post an alert card
/incident status INC-1234               show an incident's timeline
@copilot INC-1234                       ask what happened
```

Click **Investigate** on the alert card; the copilot posts a proposal into the thread with its
hypothesis, the evidence it cited, prior incidents it found, and each action's risk. **Show
evidence** opens the full audit trail. Approving is what authorises any change.

### Slack setup

`SLACK_MODE=socket` (the default) needs `SLACK_BOT_TOKEN` and an app-level `SLACK_APP_TOKEN`
(`xapp-…`, created under *Basic Information → App-Level Tokens* with `connections:write`), and no
public URL. `SLACK_MODE=http` needs `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET`, and Slack must
be pointed at `POST /slack/events`.

Bot scopes: `commands`, `chat:write`, `app_mentions:read`.

## Commands

Run `make` with no arguments for the full list.
