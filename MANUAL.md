# Incident Copilot — Operator Manual

How to set this up and run it. Written against the code as it stands on
**2026-09-03** (branch `rebuild/incident-copilot`, phases 0–2, 4, 5 complete).

This manual is kept in step with the build. See [What is not built yet](#9-what-is-not-built-yet)
for the honest boundary of what works today, and [Manual changelog](#12-manual-changelog) for how
it tracks the phases.

---

## 1. What you get today

A Slack bot that runs a real incident response loop against a simulated production stack:

```
/incident triage payments_gateway_timeout
        ↓
  🚨 alert card in the channel        [Investigate] [Ignore]
        ↓  (you click Investigate)
  Claude investigates — reads metrics, logs, change history, service state,
  searches 8 runbooks and 24 resolved past incidents
        ↓
  🔧 proposal card in the thread      [Approve & apply] [Reject] [Show evidence]
        ↓  (you click Approve)
  decision recorded, remediation queued
        ↓
  ⛔ execution + verification — NOT BUILT YET (phase 6)
```

Everything through approval works. The last hop does not — see §9.

---

## 2. Prerequisites

| Requirement | Why | Check |
|---|---|---|
| **Python 3.12+** | The package targets 3.12; 3.14 works | `python3 --version` |
| **Docker Desktop** | Runs Postgres 16 + pgvector | `docker info` |
| **Anthropic API key** | Drives the investigation | you have one, or `ant auth login` |
| **A Slack workspace** | Somewhere you can install an app | admin or app-install rights |

You do **not** need a public URL, a tunnel, or ngrok. The default transport is Socket Mode.

---

## 3. One-time setup

Run these from the repo root. Each step has a check — if the check fails, stop there.

### 3.1 Install

```bash
make install
```

✅ **Check:** `.venv/bin/python -c "import incident_copilot; print('ok')"` prints `ok`.

### 3.2 Start Postgres

```bash
make db-up
```

✅ **Check:** `docker compose ps` shows `postgres` as `running`.

> If Docker Desktop isn't running, start it first (`open -a Docker` on macOS) and wait ~10s.

### 3.3 Apply migrations

```bash
make migrate
```

✅ **Check:** `.venv/bin/alembic current` ends with `(head)`.

### 3.4 Build the search index

```bash
make index
```

✅ **Check:** prints `indexed 54 chunk(s) from seed/`.

> This loads 8 runbooks/policies and 24 resolved past incidents into Postgres full-text search.
> Re-run it any time you edit anything under `seed/`. It's idempotent.

### 3.5 Verify the whole thing

```bash
make check
```

✅ **Check:** `375 passed`. This runs lint, `mypy --strict`, and the full test suite against the
real database. It needs no Slack and no Anthropic credentials.

---

## 4. Configure credentials

Copy the template if you haven't already:

```bash
cp -n .env.example .env
```

### What each variable does

| Variable | Needed for | Notes |
|---|---|---|
| `DATABASE_URL` | everything | Default matches `docker-compose.yml`; leave it alone locally |
| `ANTHROPIC_API_KEY` | investigation | Optional if you've run `ant auth login` |
| `ANTHROPIC_MODEL` | investigation | Defaults to `claude-opus-5` |
| `AGENT_INVESTIGATE_EFFORT` | cost/quality tuning | `low`…`max`, default `high` |
| `SLACK_MODE` | transport choice | `socket` (default) or `http` |
| `SLACK_BOT_TOKEN` | Slack, both modes | `xoxb-…` |
| `SLACK_APP_TOKEN` | Slack, **socket mode only** | `xapp-…` |
| `SLACK_SIGNING_SECRET` | Slack, **http mode only** | verifies inbound requests |

### Your `.env` right now

It carries `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, and `SLACK_CHANNEL_ID` from the original
IBM DevDay project, plus `PUBLIC_BASE_URL` and `INTERNAL_BASE_URL`.

- **Keep** `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` — they belong to your **incy** app, which
  §5 updates in place rather than replacing. Re-copy the bot token after the reinstall in §5.2.
- **Delete** `SLACK_CHANNEL_ID`, `PUBLIC_BASE_URL`, and `INTERNAL_BASE_URL`. Nothing reads them;
  the channel now comes from wherever you type the slash command.
- **Add** `ANTHROPIC_API_KEY` and `SLACK_APP_TOKEN`. Neither is present, and both are required —
  the first for the agent, the second for Socket Mode.

---

## 5. Update your Slack app

You already have an app (**incy**) and your `.env` tokens come from it. Keep it — updating the
existing app is less work than creating a new one, and your signing secret stays valid.

Its current manifest is the IBM DevDay one and needs four things changed:

| Current | Why it's wrong now |
|---|---|
| `redirect_urls` → watson-orchestrate.cloud.ibm.com | Orchestrate is gone |
| `interactivity.request_url` → `…trycloudflare.com/slack/actions` | Dead tunnel, and `/slack/actions` no longer exists — this codebase serves `/slack/events` |
| `socket_mode_enabled: false` | Socket Mode is what removes the tunnel requirement entirely |
| scopes: `app_mentions:read`, `chat:write` | Missing `commands` (for `/incident`) and `chat:write.public` |
| no `slash_commands` | `/incident` is the main entry point |
| no `event_subscriptions` | `@incy` mentions need the `app_mention` bot event |

### 5.1 Replace the manifest

Go to **https://api.slack.com/apps → incy → App Manifest**, and replace the whole thing with
[`slack-app-manifest.json`](slack-app-manifest.json) in this repo:

```json
{
  "display_information": {
    "name": "incy",
    "description": "Investigates incidents, proposes remediation, waits for your approval.",
    "background_color": "#b0440e"
  },
  "features": {
    "bot_user": { "display_name": "incy", "always_online": true },
    "slash_commands": [
      {
        "command": "/incident",
        "description": "Triage and investigate incidents",
        "usage_hint": "triage payments_gateway_timeout",
        "should_escape": false
      }
    ]
  },
  "oauth_config": {
    "scopes": {
      "bot": ["commands", "chat:write", "chat:write.public", "app_mentions:read"]
    }
  },
  "settings": {
    "event_subscriptions": { "bot_events": ["app_mention"] },
    "interactivity": { "is_enabled": true },
    "socket_mode_enabled": true,
    "org_deploy_enabled": false,
    "token_rotation_enabled": false
  }
}
```

No `request_url` anywhere and no `url` on the slash command — Slack rejects the manifest if
Socket Mode is on and any of those are set. There is nothing to tunnel to.

### 5.2 Reinstall — and re-copy the bot token

Changing scopes means the app must be reinstalled: **Install App → Reinstall to Workspace**,
approve the new permissions.

⚠️ **Re-copy `xoxb-…` from *OAuth & Permissions* into `SLACK_BOT_TOKEN` afterwards.** A
reinstall can issue a new bot token, and the symptom of a stale one is `invalid_auth` at startup
rather than anything that names the cause.

Your `SLACK_SIGNING_SECRET` is unaffected — it belongs to the app, not the installation. You
don't need it at all in Socket Mode, but leave it in place for switching to HTTP later.

### 5.3 Generate the app-level token

This is the one you don't have yet, and it is **not** the bot token.

*Basic Information* → **App-Level Tokens** → **Generate Token and Scopes** → name it anything
(e.g. `socket`) → add the **`connections:write`** scope → **Generate** → copy `xapp-…`

Put it in `.env` as `SLACK_APP_TOKEN`.

> Different token, different page, different prefix. Socket Mode cannot open its WebSocket
> without it, and this is where setup usually stalls.

### 5.4 Invite the bot

In the channel you want to use:

```
/invite @incy
```

### 5.5 If you'd rather use HTTP than Socket Mode

Keep a public URL (tunnel or deploy) and set, in the manifest:

- `"socket_mode_enabled": false`
- `"interactivity": { "is_enabled": true, "request_url": "https://<host>/slack/events" }`
- `"event_subscriptions": { "request_url": "https://<host>/slack/events", "bot_events": ["app_mention"] }`
- `"url": "https://<host>/slack/events"` on the slash command

Then set `SLACK_MODE=http` in `.env`. Note the path: **`/slack/events`**, not the old
`/slack/actions`. In this mode `make api` serves Slack and `make slack` is not used.

---

## 6. Run it

Three processes, three terminals. Only the first two are needed if you're driving via HTTP
instead of Slack.

```bash
# terminal 1 — the API (health, scenario/control-plane routes)
make api

# terminal 2 — the worker (runs investigations)
make worker

# terminal 3 — Slack, Socket Mode
make slack
```

✅ **Checks:**
- `curl -s localhost:8000/readyz` → `{"status":"ready","database":"up","knowledge_base":"indexed"}`
  — a `"knowledge_base":"empty"` here means retrieval will silently return nothing; run `make index`
- terminal 2 logs `worker <host>:<pid>:<id> started`
- terminal 3 logs a Socket Mode connection and shows no traceback

> **The worker must be running.** Clicking *Investigate* only queues a job. With no worker, the
> job sits in `jobs` as `pending` and nothing appears in the thread.

---

## 7. Drive it from Slack

```
/incident list
```
Lists the five scenarios.

```
/incident triage payments_gateway_timeout
```
Posts the alert card. Click **Investigate**.

Roughly 20–60 seconds later a proposal card appears in the thread with the hypothesis, the
evidence cited, prior incidents found, and each action's risk and reversibility.

- **Show evidence** — opens a modal with the full audit trail: which tools ran, which documents
  were retrieved, token spend.
- **Approve & apply** — records the decision and queues remediation *(execution is phase 6)*.
- **Reject** — records the decision, changes nothing.

Other commands:

```
/incident status INC-1234     the incident's timeline
@Incident Copilot INC-1234    same, in a thread
```

### The five scenarios

| Scenario | Sev | What it tests |
|---|---|---|
| `payments_gateway_timeout` | SEV1 | Two safe changes that break only in combination |
| `login_outage_token_expiry` | SEV1 | A defect no config can fix — rollback is the only option |
| `latency_regression_n_plus_one` | SEV2 | Degradation ≠ outage; should *not* be called SEV1 |
| `checkout_memory_leak` | SEV2 | Restart mitigates but doesn't fix; needs a follow-up |
| `noisy_neighbor_traffic_surge` | SEV3 | Everything inside SLO — the right answer is **no action** |

Run the last one to see the bot decline to act. That's the most interesting demo.

---

## 7b. The dashboard

`http://localhost:8000/dashboard` — read-only, and the place to see *why* a proposal said what
it said.

| Page | Shows |
|---|---|
| `/dashboard` | Every incident, severity, status, and the fix that was proposed |
| `/dashboard/incidents/<KEY>` | Diagnosis, actions with risk, the tools called, token spend, the timeline, live system state, and every citation **linked to its source document** |
| `/dashboard/kb` | The whole corpus — 30 runbook/policy sections and 24 resolved incidents |
| `/dashboard/kb/<chunk-id>` | One document's text, its source file, and which incidents cited it |
| `/dashboard/scenarios` | Live derived metrics per scenario |

Approval stays in Slack, where the tokens are. Nothing on the dashboard mutates anything.

---

## 8. Driving it without Slack

The control plane is a plain REST API, which is the fastest way to see that metrics are *derived*
rather than canned:

```bash
# the incident is real
curl -s localhost:8000/scenarios/payments_gateway_timeout/metrics | python3 -m json.tool
#  error_rate 0.124, p95 1450ms, upstream_timeout_rate 0.098

# apply the correct fix
curl -s -X POST localhost:8000/scenarios/payments_gateway_timeout/actions \
  -H 'content-type: application/json' \
  -d '{"kind":"set_config_value","key":"gateway_timeout_ms","value":2000}' | python3 -m json.tool
#  health_after.healthy == true, error_rate drops to 0.002

# put it back
curl -s -X POST localhost:8000/scenarios/payments_gateway_timeout/reset > /dev/null
```

Try the *wrong* fix (`{"kind":"scale_replicas","service":"payments-api","replicas":12}`) — CPU
falls from 45% to 22.5% and the error rate doesn't budge. That's the point of the design.

Full route list: `http://localhost:8000/docs`.

---

## 9. What is not built yet

**Approving a proposal does not yet change anything.** The decision is recorded, the proposal is
marked approved, and an `execute_remediation` job is queued — but no handler is registered for
that job kind, so the worker will log it and bury it as dead:

```
ERROR job 3: no handler registered for job kind 'execute_remediation'. Registered: investigate
```

That is expected and correct for today. Phase 6 adds:

- the execute handler (applies the approved actions to the control plane)
- verification (re-reads metrics, decides whether the incident actually recovered)
- a verdict card, and re-proposal when verification fails

Also deferred: semantic retrieval (phase 3 — embeddings + reciprocal rank fusion; lexical search
is live and covers it for now), and structured logging + a scripted demo walkthrough (phase 7).

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `make db-up` hangs | Docker Desktop not running | `open -a Docker`, wait, retry |
| Tests skip with "Postgres unreachable" | DB is down | `make db-up && make migrate` |
| `/incident` does nothing in Slack | `make slack` isn't running, or the app isn't in the channel | check terminal 3; `/invite @Incident Copilot` |
| `Missing Slack configuration: SLACK_APP_TOKEN` | Socket Mode needs the app-level token | §5.3 |
| `invalid_auth` on startup | bot token went stale after a reinstall | re-copy `xoxb-…` (§5.2) |
| Manifest rejected: request URL not allowed | `socket_mode_enabled: true` with a `request_url` set | remove every `request_url` and slash-command `url` |
| Clicked Investigate, nothing appears | worker isn't running | start `make worker`; check for `pending` rows in `jobs` |
| `no handler registered for job kind 'execute_remediation'` | phase 6 isn't built | expected — see §9 |
| Investigation fails with an auth error | no Anthropic credentials | set `ANTHROPIC_API_KEY` in `.env` |
| `search_runbooks` returns nothing | index not built | `make index`; `/readyz` reports `knowledge_base: empty` |
| Proposals cite only tools, no documents | the knowledge base is empty | same — `make index` |
| "this approval has already been used" | tokens are single-use by design | run `/incident triage …` again for a fresh incident |
| Proposal card never arrives but the job succeeded | no `SLACK_BOT_TOKEN`, so posting is disabled | set it; the run still completes and is on the timeline |

### Inspecting state directly

```bash
docker compose exec postgres psql -U copilot -d copilot

\dt                                          -- tables
select key, status, severity from incidents order by created_at desc limit 5;
select id, kind, status, attempts, last_error from jobs order by id desc limit 5;
select seq, type, actor from incident_events order by id desc limit 15;
```

### Starting over

```bash
make db-reset     # destroys the volume, re-migrates, re-indexes
```

---

## 11. Reference

### Make targets

| Target | Does |
|---|---|
| `make install` | venv + dependencies |
| `make db-up` / `db-down` / `db-reset` | Postgres lifecycle |
| `make migrate` | apply migrations |
| `make index` | rebuild the runbook + history search index |
| `make api` | FastAPI on :8000 |
| `make worker` | the job worker |
| `make slack` | Slack in Socket Mode |
| `make check` | lint + types + tests (what CI runs) — safe to run while demoing; tests use a separate `copilot_test` database |
| `make test-live` | live-model tests — **costs money**, needs an API key |

### Switching to HTTP mode

Only needed if you want a publicly reachable endpoint (or eventual app distribution):

1. `SLACK_MODE=http` and set `SLACK_SIGNING_SECRET` in `.env`
2. Turn **off** Socket Mode in the app config
3. Point these three at `https://<your-host>/slack/events`: *Event Subscriptions* request URL,
   *Interactivity* request URL, and the `/incident` slash command URL
4. `make api` now serves Slack; `make slack` is not used

The handlers are identical in both modes — only the transport changes.

### Costs

Each investigation is one tool-use loop plus one structured-output call on `claude-opus-5`.
Lower `AGENT_INVESTIGATE_EFFORT` to `medium` or `low` to cut spend; the system prompt and tool
definitions are cached, so repeated runs are cheaper than the first.

---

## 12. Your checklist right now

Setup already done in this environment: Postgres is running, migrations are at head, the index is
built, and 375 tests pass. What's left is credentials and the Slack app.

- [ ] **Add `ANTHROPIC_API_KEY=sk-ant-…` to `.env`** — the only thing blocking the agent
- [ ] **Delete the dead entries from `.env`**: `SLACK_CHANNEL_ID`, `PUBLIC_BASE_URL`,
      `INTERNAL_BASE_URL` — nothing reads them
- [ ] **Replace the app manifest** with `slack-app-manifest.json` (§5.1)
- [ ] **Reinstall the app**, then **re-copy `SLACK_BOT_TOKEN`** (§5.2 — a reinstall can rotate it)
- [ ] **Generate the app-level token** and add `SLACK_APP_TOKEN=xapp-…` (§5.3)
- [ ] **`/invite @incy`** into a channel
- [ ] **`make test-live`** — worth doing before phase 6, while prompt fixes are still cheap
- [ ] **Start the three processes**, then `/incident triage payments_gateway_timeout`
- [ ] Then try `noisy_neighbor_traffic_surge` and confirm it proposes *no action*

Tell me when the live tests have run — if Claude misdiagnoses any scenario,
`incident_copilot/agent/prompts.py` is where to fix it.

---

## 13. Manual changelog

| Date | Change |
|---|---|
| 2026-09-03 | Created at the end of phase 5. Covers setup, Slack app creation, running, and the phase 6 gap. |
| 2026-09-03 | §5 rewritten to update the existing **incy** app rather than create a new one; added `slack-app-manifest.json`. |
| 2026-09-03 | Noted that the test suite now uses its own `copilot_test` database, so `make check` no longer wipes a running demo. |
| 2026-09-03 | Added §7b for the dashboard; `/readyz` now reports knowledge-base state. |

This manual is updated at the end of every phase. Phase 6 will replace §9's "not built yet" with
the execution and verification flow, and add a verdict card to §7.
