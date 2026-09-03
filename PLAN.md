# Incident Copilot — Rebuild Plan

**Status:** building — MVP track (§12), phases 0–2 and 4 complete; phase 5 (Slack) next
**Date:** 2026-09-02
**Owner:** @preyashyadav

**Build order chosen:** MVP first — phases 0→1→2→4→5→6 with lexical-only retrieval,
then phase 3 (embeddings + RRF) as an upgrade, then phase 7.

---

## 1. What this becomes

A Slack-native incident response copilot. An alert lands in a channel; an engineer asks the bot
to investigate; the bot gathers evidence, searches runbooks *and the history of past incidents*,
and comes back with a ranked proposal of concrete remediation actions plus its reasoning. The
engineer approves or rejects in the thread. On approval the bot executes the actions against a
control plane, then **re-reads the metrics and reports whether the incident actually recovered.**

The loop closes. That's the whole point — most incident-bot demos stop at "here's a summary."

### What it is demonstrating

| Skill | Where it shows up |
|---|---|
| Slack platform depth | Bolt, Block Kit, Socket Mode *and* HTTP, signature verification, 3s ack discipline, retry idempotency, single-use approval tokens |
| Async/distributed systems | Durable Postgres job queue with `SKIP LOCKED`, separate worker process, retries with backoff, at-least-once semantics |
| LLM engineering | Claude tool-use loop, read-only tool boundary, two-phase investigate→propose, structured outputs, cost/latency control via effort |
| Retrieval | Hybrid lexical + semantic search over two corpora (runbooks, resolved incident history), reciprocal rank fusion |
| Data modelling | Append-only event log per incident, proposals and executions as first-class rows, full audit trail |
| Production hygiene | Migrations, typed config, structured logs with correlation IDs, tests that don't hit the network, CI, Docker Compose |

### Explicit non-goals

- Not a real observability integration. No Datadog, no PagerDuty. The evidence comes from a simulator.
- Not multi-tenant. One workspace, one channel.
- Not an autonomous remediator. A human approves every mutation, always.

---

## 2. Decisions taken

| Decision | Choice | Why |
|---|---|---|
| LLM | Anthropic Claude (`claude-opus-5`) | Official SDK, typed tool runner, structured outputs. Replaces the hand-rolled OpenAI `/responses` loop, whose tool schemas are the wrong shape and cannot work as written. |
| Slack transport | Bolt, both adapters | HTTP (ASGI on FastAPI) is the production path — stateless, horizontally scalable, Slack retries on failure, eligible for distribution. Socket Mode is the local-dev path — no tunnel. Same handler code, `SLACK_MODE` picks one. |
| Background execution | Postgres job queue | Durable across restarts, retryable, observable. No Redis. Moving to Postgres is required for multi-replica anyway. |
| Remediation target | Simulated control plane | Self-contained, always demoable, and — critically — metrics are *derived from* control-plane state, so a correct fix visibly moves the numbers and a wrong one doesn't. |
| Database | Postgres 16 + `pgvector` | Local via Docker Compose; Neon for a hosted demo (MCP already connected). |
| Embeddings | `fastembed` (local ONNX, no API key) | Real vectors with zero extra credentials. `voyage-3` swappable behind an env var. |

### Naming

Drop the IBM/DevDay framing entirely. Service name: **Incident Copilot**, Python package
`incident_copilot`. Repo directory can stay as-is or be renamed — cosmetic, decide at the end.

---

## 3. Target architecture

```
                     ┌──────────────────────────────────────────┐
   Slack  ─────────▶ │  api (FastAPI)                           │
   (events,          │   • Bolt handlers  → ack < 3s            │
    interactions)    │   • enqueue job, return immediately      │
                     │   • REST: /healthz, /scenarios, /admin   │
                     └───────────────┬──────────────────────────┘
                                     │ INSERT INTO jobs
                                     ▼
                     ┌──────────────────────────────────────────┐
                     │  Postgres                                │
                     │   jobs · incidents · incident_events     │
                     │   proposals · action_executions          │
                     │   kb_chunks(tsvector, embedding)         │
                     │   control_plane_state                    │
                     └───────────────┬──────────────────────────┘
                                     │ SELECT … FOR UPDATE SKIP LOCKED
                                     ▼
                     ┌──────────────────────────────────────────┐
                     │  worker (separate process)               │
                     │                                          │
                     │   investigate ──▶ Claude tool loop       │
                     │                    (read-only tools)     │
                     │                      ├ get_metrics       │
                     │                      ├ get_logs          │
                     │                      ├ get_recent_changes│
                     │                      ├ search_runbooks   │
                     │                      ├ find_similar_     │
                     │                      │   incidents       │
                     │                      └ get_service_state │
                     │                                          │
                     │   propose  ──────▶ structured output     │
                     │   execute  ──────▶ control plane mutate  │
                     │   verify   ──────▶ re-read metrics       │
                     │                                          │
                     │   …each step posts to the Slack thread   │
                     └───────────────┬──────────────────────────┘
                                     ▼
                     ┌──────────────────────────────────────────┐
                     │  control plane (simulated)               │
                     │   flags · deploys · config · load        │
                     │   metrics = f(state)   ← the key idea    │
                     └──────────────────────────────────────────┘
```

### Repository layout

```
incident_copilot/
  config.py                 pydantic-settings, one typed Settings object
  db/
    models.py               SQLAlchemy 2.0 declarative
    session.py
    migrations/             Alembic
  domain/
    incident.py             lifecycle, state machine
    severity.py             classification policy
    events.py               append-only event types
  jobs/
    queue.py                enqueue / claim (SKIP LOCKED) / complete / fail
    worker.py               poll loop, backoff, graceful shutdown
    handlers/               investigate.py, execute.py, verify.py
  agent/
    tools.py                @beta_tool read-only tools
    investigate.py          tool-use loop
    propose.py              structured-output call
    schemas.py              Pydantic contracts
  retrieval/
    index.py                chunk + embed + upsert
    search.py               FTS + vector + RRF fusion
    corpora.py              runbooks, policies, resolved incidents
  controlplane/
    state.py                flags, deploys, config
    simulator.py            metrics/logs derived from state
    actions.py              the mutation catalogue
  slack/
    app.py                  Bolt app, both adapters
    handlers.py             commands, actions, mentions
    blocks.py               Block Kit builders
    approvals.py            single-use tokens, TTL
  api/
    main.py                 FastAPI, mounts Bolt + REST
scenarios/                  incident scenario packs (YAML)
seed/                       historical incidents, runbooks, policies
tests/
docker-compose.yml
Makefile
```

---

## 4. The control plane — why the demo is real

This is the piece that separates this from a scripted demo, so it's worth being precise.

The control plane holds mutable state for a fake stack:

```yaml
services:
  payments-api:
    deployed_version: v2.4.1
    previous_version: v2.4.0
    replicas: 6
feature_flags:
  enable_new_gateway: { us-east: true, eu-west: false }
config:
  gateway_timeout_ms: 1000     # was 2000
load:
  request_rate_rps: 320
```

Metrics and logs are **computed from that state**, not read from a file:

```
error_rate = base_error_rate
           + 0.11 if (enable_new_gateway[region] and gateway_timeout_ms < 1500)
           + 0.02 if deployed_version in known_bad_versions
           + surge_term(request_rate_rps)
```

Consequences that make the demo worth watching:

- Approving *"revert gateway_timeout_ms to 2000"* → error rate drops to 1.4%, verification passes.
- Approving *"scale up replicas"* → nothing improves, verification **fails**, and the bot says so
  and re-proposes. A demo that can fail correctly is far more convincing than one that can't.
- The `noisy_neighbor` scenario has no bad state at all; the correct proposal is *no action*.

Actions available to the agent (all requiring approval):

`rollback_deploy` · `toggle_feature_flag` · `set_config_value` · `scale_replicas` ·
`restart_service` · `no_action`

---

## 5. Scenario packs

Replace the single `payments_failing` fixture directory with self-contained scenario packs. Each
declares initial control-plane state, the injected fault, the change history, and the ground-truth
fix (used only by tests, never shown to the agent).

| Scenario | Sev | Fault | Correct action | Teaches |
|---|---|---|---|---|
| `payments_gateway_timeout` | SEV1 | Timeout cut 2s→1s *while* the new gateway flag is on — neither alone breaks it | `set_config_value` → 2000 | Interaction faults; two changes, one incident |
| `login_outage_token_expiry` | SEV1 | v3.1.0 reads a minutes TTL as seconds; tokens die in 60s | `rollback_deploy` | No config can fix a bad artifact |
| `latency_regression_n_plus_one` | SEV2 | ORM refactor issues one query per row; p95 10x, errors flat | `rollback_deploy` | Degradation ≠ outage; this is not a SEV1 |
| `checkout_memory_leak` | SEV2 | Heap fills after ~4h uptime | `restart_service` | Mitigation ≠ fix — recurs, needs a follow-up |
| `noisy_neighbor_traffic_surge` | SEV3 | 2.8x traffic, autoscaler already absorbed it, all within SLO | `no_action` | When *not* to act — and scaling down would cause the incident |

Verified by `tests/test_scenarios.py`: every ground-truth action resolves its scenario, and no
unrelated action does. Adding a pack adds coverage automatically.

---

## 6. Retrieval

Two corpora, one search interface.

**Corpus A — knowledge base.** Runbooks, severity policy, comms templates. Roughly 30 chunks,
markdown in `seed/kb/`.

**Corpus B — incident history.** ~40 synthetic resolved incidents in `seed/history/`, each with a
symptom signature (service, signal, metric deltas, top log patterns) and the action that actually
resolved it. This is what makes "have we seen this before?" answerable.

**Hybrid search.** For a query:

1. Lexical — Postgres `websearch_to_tsquery` + `ts_rank_cd` over a `tsvector` column.
2. Semantic — `pgvector` cosine kNN over `fastembed` embeddings.
3. Fuse with Reciprocal Rank Fusion (`score = Σ 1/(60 + rank_i)`), dedupe, return top-k.

Lexical alone handles exact identifiers (`enable_new_gateway`, `CircuitBreakerOpenException`);
semantic alone handles paraphrase ("payments are timing out" → gateway runbook). Fusion needs no
tuned weights, which is why it's the right default.

`find_similar_incidents` builds its query from the alert signature rather than raw prose, and
returns prior incidents with their resolutions and time-to-mitigate.

---

## 7. The agent

Two phases, deliberately separated.

**Phase A — investigate.** `client.beta.messages.tool_runner` with `@beta_tool` typed functions,
`claude-opus-5`, adaptive thinking, `effort: "high"`. The agent decides what to look at and in
what order. Every tool is **read-only** — the tool surface contains no mutation, so the loop
structurally cannot change anything. That boundary is a design statement, not a policy note.

**Phase B — propose.** A second call with `client.messages.parse()` against a Pydantic schema over
the gathered evidence. Structured output guarantees a valid proposal object; no JSON repair, no
`json.JSONDecodeError` fallback branch.

```python
class ProposedAction(BaseModel):
    kind: Literal["rollback_deploy", "toggle_feature_flag", "set_config_value",
                  "scale_replicas", "restart_service", "no_action"]
    target: str
    params: dict[str, str | int | bool]
    rationale: str
    reversible: bool
    risk: Literal["low", "medium", "high"]

class IncidentProposal(BaseModel):
    severity: Literal["SEV1", "SEV2", "SEV3"]
    hypothesis: str
    confidence: Literal["low", "medium", "high"]
    evidence_cited: list[str]        # tool call ids / chunk ids — traceable
    similar_incidents: list[str]
    actions: list[ProposedAction]    # ordered, may be empty for no_action
    verification_plan: str
    next_update_minutes: int
```

Splitting the phases means the tool loop is never fighting an output constraint, evidence
gathering and judgement are separately testable, and `evidence_cited` gives every claim a
provenance trail back to a specific tool result or KB chunk.

Cost control: `effort: "high"` for investigation, `low` for the Slack-formatting pass. Prompt
caching on the system prompt + tool definitions, which are byte-stable across every incident.

---

## 8. Slack

**Transport.** One set of Bolt handlers, two adapters:

```python
if settings.slack_mode == "socket":
    SocketModeHandler(bolt_app, settings.slack_app_token).start()
else:
    api.mount("/slack", SlackRequestHandler(bolt_app))   # ASGI, signature-verified
```

**Interaction surface.**

- `/incident triage <scenario>` — inject a scenario, post the alert card.
- Button **Investigate** on the alert card — enqueues a job, acks immediately.
- Threaded progress: "🔍 gathering evidence" → "📚 searched 4 runbooks, found 2 similar incidents" → proposal card.
- Proposal card — actions as rows with risk badges, buttons **Approve all** / **Approve selected** / **Reject** / **Explain**.
- **Explain** opens a modal with the evidence trail and cited chunks.
- On approval: execution progress, then a verification verdict card (recovered / not recovered / partial).
- `@copilot what happened with INC-1234` — answers from the event log.

**Correctness details that matter more than the feature list:**

- *Ack within 3 seconds, always.* Handlers do nothing but validate, enqueue, and return.
- *Approval tokens are server-side and single-use.* Today `slack.py:198` json-loads the button's
  `value` and feeds it straight into the workflow — that payload is client-controlled. Replace it
  with an opaque `proposal_id` + nonce, looked up server-side, marked consumed on first use, TTL 30
  minutes. Prevents replay and stops a stale card from re-firing an action.
- *Retries are idempotent.* Slack redelivers on any non-2xx. Dedupe on
  `(event_id, X-Slack-Retry-Num)` in a `slack_deliveries` table; jobs carry an idempotency key.
- *Reject the clock skew window.* Timestamps older than 5 minutes fail verification.

---

## 9. Job queue

```sql
CREATE TABLE jobs (
  id           bigserial PRIMARY KEY,
  kind         text NOT NULL,
  payload      jsonb NOT NULL,
  idem_key     text UNIQUE,
  status       text NOT NULL DEFAULT 'pending',
  attempts     int  NOT NULL DEFAULT 0,
  max_attempts int  NOT NULL DEFAULT 3,
  run_after    timestamptz NOT NULL DEFAULT now(),
  locked_at    timestamptz,
  locked_by    text,
  last_error   text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON jobs (status, run_after) WHERE status = 'pending';
```

Claim is the standard pattern:

```sql
UPDATE jobs SET status='running', attempts=attempts+1,
       locked_at=now(), locked_by=$1
WHERE id = (SELECT id FROM jobs
            WHERE status='pending' AND run_after<=now()
            ORDER BY run_after LIMIT 1
            FOR UPDATE SKIP LOCKED)
RETURNING *;
```

Failure re-queues with exponential backoff (`run_after = now() + 2^attempts · 30s`); exhausted
attempts move to `dead` and post a failure card to the thread. A reaper releases locks held longer
than the visibility timeout, so a killed worker doesn't strand a job. At-least-once delivery is
assumed throughout — handlers are written idempotent.

---

## 10. Data model

- `incidents` — identity, classification, current status, Slack channel/thread refs.
- `incident_events` — **append-only.** Every state change is an event (`created`, `investigation_started`, `evidence_gathered`, `proposal_created`, `approved`, `rejected`, `action_executed`, `verified`, `resolved`). The incident's current state is a projection. Gives a free audit trail, a free timeline for the `@copilot what happened` command, and a free postmortem source.
- `proposals` — the structured output, verbatim, plus status and approver.
- `action_executions` — one row per action, with before/after control-plane snapshots.
- `kb_chunks` — content, `tsvector`, `vector(384)`, source metadata.
- `control_plane_state` — current state, versioned so a scenario can be reset.

---

## 11. Testing

Network is never touched in tests.

- **Control plane / simulator** — pure functions, straightforward unit tests.
- **Retrieval** — a golden set of ~25 (query → expected chunk) pairs; assert recall@3. Catches
  regressions when chunking or fusion changes.
- **Agent** — record/replay. Real Claude responses captured once into cassettes, replayed in CI.
  Plus a live-API test suite behind `-m live`, run manually.
- **Scenario end-to-end** — for each pack, drive the full loop with a fake Slack client and assert
  the proposal's top action matches the pack's ground-truth fix, and that verification passes after
  execution. This is the suite that proves the thing actually works.
- **Slack** — signature verification (valid, tampered, stale), retry idempotency, token replay
  rejection, 3-second ack budget.
- **Queue** — concurrent claim doesn't double-deliver; crashed worker's job is reclaimed.

`ruff` + `mypy --strict` on `incident_copilot/` + `pytest` in GitHub Actions.

---

## 12. Phases

Each phase leaves the repo in a working state.

| # | Phase | Delivers | Est. |
|---|---|---|---|
| 0 | ✅ **Teardown & foundation** | IBM/Orchestrate artifacts deleted (`openapi.json`, `openapiv2.json`, the `.pages` doc, approvals polling bridge). New package layout, `pydantic-settings` config, Docker Compose (postgres+pgvector), Alembic baseline, Makefile, CI. | 0.5d |
| 1 | ✅ **Control plane & scenarios** | State model, metrics/logs simulator, action catalogue, 5 scenario packs, reset endpoint. Unit tested. | 1d |
| 2 | ✅ **Data model & job queue** | All tables, event log, queue with SKIP LOCKED, worker process, backoff, reaper. Concurrency tests. | 1d |
| 3 | ◑ **Retrieval** | KB + history corpora authored, chunking, recall@3 golden set — **lexical half done in phase 4**. Remaining: fastembed embeddings, pgvector column, RRF fusion. | 0.5d |
| 4 | ✅ **Agent** | Read-only tool surface, investigate loop, structured proposal, prompt caching, cassette tests. | 1d |
| 5 | **Slack** | Bolt with both adapters, alert/proposal/verification cards, approval tokens, idempotency, modal. | 1.5d |
| 6 | **Execute & verify** | Approval → execution → re-read metrics → recovery verdict → re-propose on failure. Full E2E per scenario. | 1d |
| 7 | **Polish** | README with a scripted demo walkthrough, architecture diagram, structured logging, seed history expanded to 40, `pgvector` tuning. | 0.5d |

**≈ 7.5 focused days.**

### MVP cut line

If the goal is something demoable fast, phases **0 → 1 → 2 → 4 → 5 → 6** with retrieval reduced to
lexical-only (skip embeddings, defer phase 3) is roughly **4 days** and still shows the complete
close-the-loop story. Phase 3 then becomes a visible upgrade rather than a prerequisite.

---

## 13. Things being deliberately deleted

- `openapi.json`, `openapiv2.json` — Orchestrate-facing contracts, one of which already documents a
  route (`/slack/thread_update`) that does not exist.
- `Incident Report Product.pages` — 317KB binary, hackathon artifact.
- `approvals_api.py` / `approvals_store.py` — an in-memory polling queue for an external
  orchestrator. Replaced by the real approval flow.
- `incident_runner.py` — the fixture-only fallback path. The control plane replaces it.
- `agent.py` / `agent_tools.py` — hand-rolled OpenAI loop with tool schemas in the wrong shape for
  `/responses`. Replaced wholesale.
- The `main.py` / `agent_tools.py` duplication — one domain layer, called by both surfaces.
- SQLite. Postgres from phase 0.

**Kept:** `incident_logic.py`'s severity and assignee policy (good, small, testable), the fixture
JSON as the seed for `payments_gateway_timeout`, and the general shape of `_format_incident_text`
as a starting point for Block Kit.

---

## 14. Open questions

1. **Where does this run for a demo?** Local Docker Compose is enough for Socket Mode. If you want
   a live HTTP endpoint, Neon + a small Fly/Render deployment — decide by phase 5.
2. **Repo rename?** `incident-evidence-service` no longer describes it. Cosmetic; decide at the end.
3. **Does the seeded history get generated or hand-written?** Generated with Claude in a one-off
   script, reviewed, then committed as static JSON — so it's deterministic in CI.

---

## 15. Next step

Start phase 0. Nothing in it depends on an unanswered question.
