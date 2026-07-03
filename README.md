# Devin Automation for Apache Superset

An event-driven automation that watches a GitHub fork of `apache/superset` for issues labeled `devin-fix`, hands each one to a [Devin](https://devin.ai) session to investigate and fix, and tracks progress on a live dashboard until a pull request is opened.

## How it works

```
┌─────────────────┐      poll every 60s      ┌──────────────────┐
│  GitHub (fork)   │ ───────────────────────► │  github_poller()  │
│  issues labeled  │                           └────────┬─────────┘
│  "devin-fix"     │                                    │ new issue found
└─────────────────┘                                     ▼
                                              process_issue_remediation()
                                                          │
                                                          ▼
                                              ┌──────────────────────┐
                                              │   Devin session       │
                                              │   created via API     │
                                              └───────────┬───────────┘
                                                          │
                          poll every 20s                 ▼
┌──────────────────┐ ◄─────────────────────  ┌──────────────────────┐
│  status_poller()  │                          │  SQLite (IssueTask)  │
└──────────────────┘ ───────────────────────► └──────────┬───────────┘
                                                          │
                                                          ▼
                                              ┌──────────────────────┐
                                              │  Dashboard  (GET /)   │
                                              │  metrics + task table │
                                              └──────────────────────┘
```

Two independent background loops run for the lifetime of the app (started in `main.py`'s
`lifespan`):

- **`github_poller()`** — every `POLL_INTERVAL_SECONDS` (60s), checks the configured repo for:
  1. Open issues labeled `devin-fix` that don't have a task yet → triggers a new Devin session.
  2. Comments containing `@devin` on an already-labeled issue → re-triggers a session using the
     comment as instructions (only comments posted after the app started are considered).
- **`status_poller()`** — every 20s, polls Devin for the current status of every non-terminal task
  and updates the DB. Once Devin reports a pull request, the task is marked `complete`.

Each triggered issue gets one row in a local SQLite DB (`devin_automation.db`), rendered on the
dashboard at `http://localhost:8000/`.

> **Note on triggering:** the app currently triggers *only* via polling. There's a `/webhook`
> route in `main.py` for instant, event-driven triggering from a real GitHub webhook, but it's
> commented out (see [Re-enabling the webhook](#re-enabling-the-webhook) below) and
> `github_client.py`'s signature verification isn't imported anywhere right now. Polling was kept
> as the active path since it doesn't require the app to be publicly reachable.

## Project structure

```
.
├── main.py                  # FastAPI app: pollers, dashboard, /send_input
├── devin_client.py          # Thin wrapper around the Devin API
├── github_client.py         # GitHub webhook signature verification (currently unused, see above)
├── renderer.py              # Dashboard metric card HTML
├── requirements.txt         # Runtime dependencies
├── requirements-dev.txt     # + test dependencies
├── pytest.ini
├── Dockerfile
├── compose.yaml
├── test_simulation.py       # Fully-mocked end-to-end demo script
└── tests/
    ├── conftest.py
    ├── test_main.py
    ├── test_github_client.py
    └── test_renderer.py
```

## Setup

### 1. Fork the target repo

Fork `apache/superset`, or any repository you want to use this automation tool on into your own GitHub org/account. Create `devin-fix` and `devin-complete` labels if they do not exist in this repository already. This particular tool can be used for any kind of Create one or more issues in your fork representing the work you want Devin to remediate (dependency bumps, small bugs, lint issues, etc.), and label each one `devin-fix`.

### 2. Environment variables

Create a copy of the  `.env.example` file in the project root and fill in all the following fields:

```bash
# Devin API
DEVIN_ORG_ID=org-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DEVIN_API_KEY=your_devin_api_key

# GitHub polling
GITHUB_TOKEN=ghp_your_personal_access_token
GITHUB_REPO=your-org/superset                 # "owner/repo" of your fork

# Only needed if you re-enable the webhook route (see below)
GITHUB_SECRET=your_webhook_secret
```

`GITHUB_TOKEN` needs at least `repo` scope (read access to issues/comments on your fork) to poll a repository successfully.

<!-- ### 3. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
``` -->

## Running it with Docker

```bash
docker compose up --build
```

### Triggering it for real

With the app running and `GITHUB_REPO`/`GITHUB_TOKEN` set:

1. Open (or label) an issue in your fork with `devin-fix`.
2. Within `POLL_INTERVAL_SECONDS` (30s), `github_poller` picks it up and creates a Devin session.
3. Watch it progress on the dashboard — status updates every 20s.
4. To nudge an in-progress session, use the **💬 Input** button on the dashboard row to send a message
   directly to that session.
5. When Devin opens a PR, the task flips to `devin-complete` and the PR link appears in the table.

## Simulating the workflow (no credentials needed)

To sanity-check the whole pipeline without a real GitHub repository or Devin API key:

```bash
python test_simulation.py
```

This mocks `create_session` and `get_session` and runs the actual `process_issue_remediation()`
and dashboard-rendering code against a throwaway `simulation.db`, printing each stage:

```
🚀 Starting Devin Automation Simulation (fully mocked, no network calls)

Step 1: Simulating a new 'devin-fix' labeled issue being picked up by github_poller...
  [mock devin] create_session called for repo=https://github.com/your-org/superset
  Task created -> status=IssueBaseStatus.NEW, session_id=sim-session-001

Step 2: Simulating status_poller picking up Devin's completion...
  [mock devin] get_session called for sim-session-001
  Task updated -> status=IssueBaseStatus.COMPLETE, pr_url=https://github.com/your-org/superset/pull/42

Step 3: Rendering the dashboard with this task...
  Dashboard rendered successfully and contains the completed task ✅

✅ Simulation successful — full lifecycle verified:
   trigger -> Devin session created -> status polled -> PR linked -> shown on dashboard
```

Good for a quick demo recording, or as a smoke test after pulling a fresh checkout.


## Observability

The dashboard's metric cards answer the "is this actually working" question at a glance:

| Metric | What it tells you |
|---|---|
| Issues Triggered | Total volume through the pipeline |
| Completed | Count + % that reached `complete` |
| PRs Opened | Count + % that produced a pull request |
| Active / In Progress | Currently running or waiting on a human |
| Errors / Blocked | Count + % across all failure/limit states |
| Avg Turnaround | Hours from issue created → completed |

## Known limitations / next steps

- **Webhook is disabled.** Polling was chosen so the app doesn't need a public URL for this demo.
  To re-enable instant, event-driven triggering:
  1. Uncomment the `/webhook` route in `main.py`.
  2. Add back `from github_client import verify_github_signature`.
  3. Point a GitHub webhook (content type `application/json`) at `https://your-host/webhook` with events `issues` and `issue_comment`, and set `GITHUB_SECRET` to match.
- **This can be extended to tools such as Slack and Microsoft Teams.** A chatbot can be created such that with proper API permissions and webhooks in place, a user would be able to tag the chatbot along with an issue ID and Devin would be able to fix that issue.
