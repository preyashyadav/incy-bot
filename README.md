# Incident Evidence Service

FastAPI service for incident response demos. It handles incident intake, evidence retrieval from fixtures, KB search, Slack-triggered workflows, and an OpenAI-powered agent that uses internal tools.

<img width="3024" height="1964" alt="image" src="https://github.com/user-attachments/assets/bd03a763-e15d-49a9-a337-4e4721520505" />


## Features
- Incident lifecycle endpoints: create, assign, evidence, notes.
- KB search with SQLite FTS5 chunks.
- Slack alert posting and interactive approvals.
- OpenAI agent loop that calls local tools (create incident, fetch evidence, KB search, notes).

## Quick Start
1. Create a virtual environment and install dependencies.
2. Configure env vars (see `.env.example`).
3. Run the API.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Environment Variables
Required for OpenAI agent:
- `OPENAI_API_KEY`

Optional OpenAI settings:
- `OPENAI_MODEL` (default `gpt-4o-mini`)
- `OPENAI_TIMEOUT` (seconds, default `20`)
- `OPENAI_BASE_URL` (default `https://api.openai.com/v1`)

Required for Slack:
- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`
- `SLACK_CHANNEL_ID`

Notes:
- `.env` is loaded automatically by `app/main.py`.
- Never commit secrets; use `.env.example` as a template.

## API Overview
Core endpoints:
- `POST /incidents`
- `POST /incidents/{incident_id}/assign`
- `GET /incidents/{incident_id}/evidence`
- `POST /incidents/{incident_id}/notes`
- `GET /kb/search`
- `POST /incident/start`
- `POST /approvals`
- `GET /approvals/next`
- `POST /slack/alert`
- `POST /slack/actions`

OpenAPI specs:
- `openapi.json`
- `openapiv2.json`

## How the Agent Works
The OpenAI agent in `app/agent.py`:
1. Receives the alert payload.
2. Calls internal tools via function calls:
   - create incident
   - assign owners
   - fetch evidence
   - search KB
   - add notes
3. Returns a structured JSON response for Slack.

Slack flow:
- `/slack/alert` posts the interactive alert message.
- `/slack/actions` runs the agent and posts the incident summary to the thread.

If `OPENAI_API_KEY` is not set, Slack actions fall back to fixture-only behavior.

## Data Storage
- SQLite DB at `app/incidents.db`
- KB chunks stored in SQLite FTS5 (`kb_chunks` table)

## Development Notes
- Fixtures live in `app/fixtures/payments_failing`.
- Only `payments_failing` fixtures exist by default; other incident types will return missing-fixture errors.

