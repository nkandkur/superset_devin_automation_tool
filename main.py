import os
import requests
import traceback

import asyncio
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import FastAPI, Request, HTTPException, Header, BackgroundTasks, Form
from fastapi.responses import HTMLResponse
from enum import Enum
from dotenv import load_dotenv
from sqlmodel import SQLModel, Field, create_engine, Session, select
from renderer import render_metric_cards
from devin_client import *
from contextlib import asynccontextmanager



# GitHub polling config (used as an alternative to webhooks — see github_poller())
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
POLL_LABEL = os.getenv("POLL_LABEL", "devin-fix")
POLL_INTERVAL_SECONDS = 5
LAST_COMMENT_POLL = datetime.now(timezone.utc)  # only look at comments created after startup

# --- Configuration ---
DATABASE_URL = "sqlite:///./devin_automation.db"

class IssueBaseStatus(str, Enum):
    NEW = "new"
    CLAIMED = "claimed"
    RUNNING = "running"
    EXIT = "exit"
    ERROR = "error"
    SUSPENDED = "suspended"
    RESUMING = "resuming"
    COMPLETE = "complete"

class IssueStatusDetail(str, Enum):
    WORKING = "working"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    FINISHED = "finished"
    INACTIVITY = "inactivity"
    USER_REQUEST = "user_request"
    USAGE_LIMIT_EXCEEDED = "usage_limit_exceeded"
    OUT_OF_CREDITS = "out_of_credits"
    OUT_OF_QUOTA = "out_of_quota"
    NO_QUOTA_ALLOCATION = "no_quota_allocation"
    PAYMENT_DECLINED = "payment_declined"
    ORG_USAGE_LIMIT_EXCEEDED = "org_usage_limit_exceeded"
    TOTAL_SESSION_LIMIT_EXCEEDED = "total_session_limit_exceeded"
    ERROR = "error"
    NA = "NA"

STATUS_DETAIL_LABELS = {
    IssueStatusDetail.WORKING: "Working",
    IssueStatusDetail.WAITING_FOR_USER: "Waiting for You",
    IssueStatusDetail.WAITING_FOR_APPROVAL: "Waiting for Approval",
    IssueStatusDetail.FINISHED: "Finished",
    IssueStatusDetail.INACTIVITY: "Paused (Inactivity)",
    IssueStatusDetail.USER_REQUEST: "Stopped by Request",
    IssueStatusDetail.USAGE_LIMIT_EXCEEDED: "Usage Limit Exceeded",
    IssueStatusDetail.OUT_OF_CREDITS: "Out of Credits",
    IssueStatusDetail.OUT_OF_QUOTA: "Out of Quota",
    IssueStatusDetail.NO_QUOTA_ALLOCATION: "No Quota Allocated",
    IssueStatusDetail.PAYMENT_DECLINED: "Payment Declined",
    IssueStatusDetail.ORG_USAGE_LIMIT_EXCEEDED: "Org Usage Limit Exceeded",
    IssueStatusDetail.TOTAL_SESSION_LIMIT_EXCEEDED: "Session Limit Exceeded",
    IssueStatusDetail.ERROR: "Error",
    IssueStatusDetail.NA: "N/A",
    IssueBaseStatus.NEW: "New",
    IssueBaseStatus.CLAIMED: "Claimed",
    IssueBaseStatus.RUNNING: "Running",
    IssueBaseStatus.EXIT: "Exit",
    IssueBaseStatus.ERROR: "Error",
    IssueBaseStatus.SUSPENDED: "Suspended",
    IssueBaseStatus.RESUMING: "Resuming",
    IssueBaseStatus.COMPLETE: "Complete"
}

ERROR_STATUSES = {
    IssueBaseStatus.ERROR,
    IssueBaseStatus.SUSPENDED,
    IssueStatusDetail.ERROR,
    IssueStatusDetail.TOTAL_SESSION_LIMIT_EXCEEDED,
    IssueStatusDetail.ORG_USAGE_LIMIT_EXCEEDED,
    IssueStatusDetail.PAYMENT_DECLINED,
    IssueStatusDetail.NO_QUOTA_ALLOCATION,
    IssueStatusDetail.OUT_OF_QUOTA,
    IssueStatusDetail.OUT_OF_CREDITS,
    IssueStatusDetail.USAGE_LIMIT_EXCEEDED,
    IssueStatusDetail.INACTIVITY
    
}
ACTIVE_STATUSES = {
    IssueBaseStatus.NEW,
    IssueBaseStatus.CLAIMED,
    IssueBaseStatus.RUNNING,
    IssueBaseStatus.RESUMING,
    IssueStatusDetail.WAITING_FOR_APPROVAL,
    IssueStatusDetail.WAITING_FOR_USER
}

FINISHED_STATUSES = {
    IssueBaseStatus.EXIT,
    IssueBaseStatus.COMPLETE   
}

# --- Database Setup ---
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

class IssueTask(SQLModel, table=True):
    issue_number: int = Field(primary_key=True)
    issue_url: str
    short_description: str
    session_id: str
    status: IssueBaseStatus
    status_detail: IssueStatusDetail
    pr_url: Optional[str] = None
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

SQLModel.metadata.create_all(engine)

# --- Core Logic ---
async def process_issue_remediation(issue_number: int, title: str, body: str, repo_url: str, issue_url: str):
    # Claim the issue immediately, before calling Devin, so a concurrent
    # poll cycle (or a slow-to-respond Devin call spanning multiple poll
    # intervals) can't double-trigger a session for the same issue.
    # If a task already exists, this is a legitimate re-trigger (e.g. an
    # "@devin" comment mention) and we fall through to update it below.
    with Session(engine) as claim_session:
        existing = claim_session.exec(
            select(IssueTask).where(IssueTask.issue_number == issue_number)
        ).first()
        if not existing:
            try:
                placeholder = IssueTask(
                    issue_number=issue_number,
                    issue_url=issue_url,
                    short_description=title,
                    session_id="pending",
                    status=IssueBaseStatus.NEW,
                    status_detail=IssueStatusDetail.NA
                )
                claim_session.add(placeholder)
                claim_session.commit()
            except Exception as e:
                claim_session.rollback()
                print(f"Issue {issue_number} already claimed by a concurrent trigger, skipping")
                print(e)
                return

    with Session(engine) as session:
        try:
            prompt = f"""
            You are an experienced software engineer contributing to a fork of Apache Superset. Your objective is to completely resolve the GitHub issue provided below while following the repository's contribution guidelines and engineering best practices.

            ## Repository

            Repository URL: {repo_url}

            GitHub Issue: {issue_url}
            - Description: {body}

            0. Create a comment on the issue with the link to the Devin session like this: "Devin is working on this issue [here](_DEVIN_SESSION_URL)".
            1. Read the GitHub issue and summarize the important points (e.g. current behavior, expected behavior, observed error, reproduction steps, affected subsystems).
            - If the issue description is ambiguous, infer the most reasonable interpretation based on the current state of the repository rather than immediately asking for clarification.
            2. Clone the repository and inspect its structure, such as developer and test documentation, and formatting requirements. Determine the locations of the relevant subsystems.
            3. Checkout to a new branch called scratch/{issue_number}.
            4. Setup the development environment according to the repository's contribution guidelines.
            5. Reproduce the bug and perform root cause analysis based on what is observed during reproduction of the bug. Explain why the bug occurs, and why existing code behaves incorrectly. Avoid any speculative fixes.
            6. Implement the smallest possible fix that fully resolves the issue. Prioritize correctness, maintainability, and readability. Avoid making unrelated changes that could lead to regressions.
            - If multiple solutions exist, choose the simpler solution.
            7. Validate the fix by running the relevant tests. Assert that the expected behavior is restored and no new regressions are introduced. Fix the fix if this validation fails.
            8. Add a regression test if applicable. You may update an existing test if that happens to be less invasive.
            9. Run checks to ensure there is proper formatting/linting for the code changes.
            10. Generate a pull request description with the following format:
            - ### Issue: 

                _URL to the issue_

                ### What has changed:

                _A clear technical description of what has changed_

                ### Why is this change needed:

                _Clearly describes why this changed was needed_

                ### Screenshots / visuals: 

                _eg Screenshots etc_

                ### Developer Testing:

                - [ ] _I've updated/added unit and/or IT tests (not just annotating existing test)_
                    - Tests ran: _Tests ran (include references to the code itself)_

                ### How has the design changed:

                _Decribes if this PR goes further than what is defined by the issue_

                ### Todo/Notes:

                _Any pending action that may be needed as a result of these changes_
                
                Link to Devin session: _LINK_

            11. Create a pull request against the main branch.
            12. Add the 'devin-complete' label to the issue once the pull request is opened.

            
            Work autonomously throughout this process, please do not wait for confirmation after each step.

            Only stop if there is exceptional circumstances that prevent progress, such as missing credentials or access, or if the issue cannot be properly reproduced.
            If blocked, please explain what blocked progress, what was attempted, what information or access is needed, and recommended next steps.
            Otherwise, continue until the issue is fully resolved, validated, and documented.
            """
            print(f"[{issue_number}] Sending request to create Devin session for {repo_url}...")
            devin_response = await create_session({
                "prompt": prompt,
                "repository": repo_url,
            })

            if not devin_response:
                raise Exception("Failed to create Devin session")

            print(f"[{issue_number}] Devin API responded: {devin_response}")

            session_id = devin_response.get("session_id")
            status = IssueBaseStatus(devin_response.get("status"))

            # Claim phase above guarantees this row already exists — just update it
            task = session.exec(select(IssueTask).where(IssueTask.issue_number == issue_number)).first()
            task.session_id = session_id
            task.status = status

            session.add(task)
            session.commit()
            print(f"[{issue_number}] Successfully updated database with session_id: {session_id}")
        except Exception as e:
            print(f"[{issue_number}] ERROR processing issue: {e}")
            traceback.print_exc()
            session.rollback()
            task = session.exec(select(IssueTask).where(IssueTask.issue_number == issue_number)).first()
            if task:
                task.status = IssueBaseStatus.ERROR
                session.commit()

# --- Background Poller ---
async def status_poller():
    while True:
        with Session(engine) as session:
            try:
                active_tasks = session.exec(
                    select(IssueTask).where(IssueTask.status.in_(ACTIVE_STATUSES))
                ).all()
                print(f"[status_poller] Checking status for {len(active_tasks)} active tasks...")
                for task in active_tasks:
                    # Poll session status
                    status_data = await get_session(task.session_id)
                    if status_data:
                        print(f"[status_poller] Issue {task.issue_number} changed status: {task.status} -> {status_data.get('status')}")
                        task.status = IssueBaseStatus(status_data.get("status", IssueBaseStatus.RESUMING.value))
                        if status_data.get("status_detail"):
                            task.status_detail = IssueStatusDetail(status_data.get("status_detail"))
                        else:
                            task.status_detail = IssueStatusDetail.NA
                        all_prs = status_data.get("pull_requests")
                        if all_prs:
                            print(f"[status_poller] PR found for issue {task.issue_number}: {all_prs[-1].get('pr_url')}")
                            task.pr_url = all_prs[-1].get("pr_url")
                            task.status = IssueBaseStatus.COMPLETE
                            task.status_detail = IssueStatusDetail.FINISHED
                        task.updated = datetime.now(timezone.utc)
                session.commit()
            except Exception as e:
                print(f"Poller error: {e}")
        await asyncio.sleep(8)

# --- GitHub Poller (alternative trigger to webhooks) ---
async def github_poller():
    """
    Periodically polls the configured GitHub repo for two trigger conditions,
    mirroring the webhook's `issues` and `issue_comment` handlers:

      1. Open issues labeled `devin-fix` that aren't in the DB yet.
      2. Comments containing "@devin" on issues that carry the `devin-fix`
         label — re-triggers a session using the comment as instructions.

    This is a scheduled/periodic trigger, used in place of a webhook so the
    app doesn't need to be publicly reachable. Swap in the /webhook route
    for lower-latency, event-driven triggering in a real deployment.
    """
    global LAST_COMMENT_POLL
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    while True:
        print(f"[github_poller] Checking GitHub for new issues labeled {POLL_LABEL}...")
        try:
            if not GITHUB_REPO:
                print("GITHUB_REPO not set — skipping GitHub poll")
            else:
                # --- Trigger path 1: newly labeled issues ---
                response = requests.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/issues",
                    headers=headers,
                    params={"labels": POLL_LABEL, "state": "open", "per_page": 100},
                )

                if response.status_code != 200:
                    print(f"[github_poller] GitHub API Error: {response.status_code} - {response.text}")
                else:
                    issues = response.json()
                    with Session(engine) as session:
                        for issue in issues:
                            labels = [l.get("name") for l in issue.get("labels", [])]
                            print(labels)
                            if "pull_request" in issue or 'devin-complete' in labels:
                                continue  # the /issues endpoint also returns PRs, skip those

                            issue_number = issue["number"]
                            existing = session.exec(
                                select(IssueTask).where(IssueTask.issue_number == issue_number)
                            ).first()
                            if existing:
                                continue  # already triggered, don't double-fire

                            print(f"[github_poller] Found new issue #{issue_number} - Triggering remediation...")
                            asyncio.create_task(process_issue_remediation(
                                issue_number=issue_number,
                                title=issue["title"],
                                body=issue.get("body") or "",
                                repo_url=f"https://github.com/{GITHUB_REPO}",
                                issue_url=issue["html_url"],
                            ))

                # --- Trigger path 2: "@devin" mentions in new comments ---
                poll_time = datetime.now(timezone.utc)
                comments_resp = requests.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/issues/comments",
                    headers=headers,
                    params={
                        "since": LAST_COMMENT_POLL.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "sort": "created",
                        "direction": "asc",
                        "per_page": 100,
                    },
                )

                if comments_resp.status_code != 200:
                    print(f"[github_poller] GitHub API Error: {comments_resp.status_code} - {comments_resp.text}")
                else:
                    for comment in comments_resp.json():
                        body = comment.get("body") or ""
                        if "@devin" not in body:
                            continue

                        # issue_url looks like https://api.github.com/repos/{owner}/{repo}/issues/{number}
                        issue_number = int(comment["issue_url"].rstrip("/").split("/")[-1])
                        issue_resp = requests.get(comment["issue_url"], headers=headers)
                        if issue_resp.status_code != 200:
                            continue

                        issue = issue_resp.json()
                        if "pull_request" in issue:
                            continue

                        labels = [l.get("name") for l in issue.get("labels", [])]
                        if POLL_LABEL not in labels:
                            continue  # matches webhook behavior: label required to act on mentions

                        print(f"[github_poller] Detected @devin mention on issue #{issue_number} - Re-triggering...")
                        asyncio.create_task(process_issue_remediation(
                            issue_number=issue_number,
                            title=issue["title"],
                            body=body,
                            repo_url=f"https://github.com/{GITHUB_REPO}",
                            issue_url=issue["html_url"],
                        ))

                    LAST_COMMENT_POLL = poll_time
        except Exception as e:
            print(f"GitHub poller error: {e}")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# --- FastAPI App ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    print("Starting status_poller task...")
    asyncio.create_task(status_poller())
    print("Starting github_poller task...")
    asyncio.create_task(github_poller())
    
    yield
    
    
app = FastAPI(title="Devin Automation Server", lifespan=lifespan)


# @app.post("/webhook")
# async def github_webhook(
#     request: Request, 
#     background_tasks: BackgroundTasks,
#     x_hub_signature_256: Optional[str] = Header(None)
# ):
#     body = await request.body()
#     verify_github_signature(body, x_hub_signature_256 or "")
#     data = await request.json()
#     event = request.headers.get("X-GitHub-Event")

#     should_trigger = False
#     issue_data = {}
    
#     labels = []
#     issue = data.get("issue", {})
#     if issue:
#         labels = [l.get("name") for l in issue.get("labels", [])]

#     if event == "issues":
#         action, issue = data.get("action"), data.get("issue")
#         if (action == "opened" or action == "labeled") and "devin-fix" in labels:
#             should_trigger = True
#             issue_data = {
#                 "issue_number": issue.get("number"),
#                 "title": issue.get("title"),
#                 "body": issue.get("body"),
#                 "issue_url": issue.get("html_url"),
#             }

#     elif event == "issue_comment":
#         comment = data.get("comment")
#         if "@devin" in comment.get("body", "") and "devin-fix" in labels:
#             should_trigger = True
#             issue = data.get("issue")
#             issue_data = {
#                 "issue_number": issue.get("number"),
#                 "title": issue.get("title"),
#                 "body": comment.get("body"),
#                 "issue_url": issue.get("html_url"),
#             }

#     if should_trigger:
#         repo_url = data.get("repository", {}).get("html_url")
#         background_tasks.add_task(process_issue_remediation, **issue_data, repo_url=repo_url)
#         return {"message": "Devin triggered"}

#     return {"message": "Ignored"}

@app.post("/send_input")
async def send_input(session_id: str = Form(...), user_input: str = Form(...)):
    """Sends user input to a specific Devin session."""
    # Assuming /sessions/{id}/input is the endpoint to send messages
    response = await send_message(devin_id=session_id, payload={"message": user_input})
    if not response:
        raise HTTPException(status_code=500, detail="Failed to send input to Devin")
    return {"status": "sent"}

def compute_metrics(tasks: List["IssueTask"]) -> dict:
    total = len(tasks)
    completed = [t for t in tasks if t.status in FINISHED_STATUSES]
    errored = [t for t in tasks if t.status in ERROR_STATUSES]
    active = [t for t in tasks if t.status in ACTIVE_STATUSES]
    prs_opened = [t for t in tasks if t.pr_url]

    def pct(n):
        return round((n / total) * 100, 1) if total else 0.0

    turnaround_hours = [
        (t.updated - t.created).total_seconds() / 3600
        for t in completed
        if t.updated and t.created
    ]
    avg_turnaround = round(sum(turnaround_hours) / len(turnaround_hours), 1) if turnaround_hours else None

    return {
        "total": total,
        "completed": len(completed),
        "completed_pct": pct(len(completed)),
        "active": len(active),
        "errored": len(errored),
        "errored_pct": pct(len(errored)),
        "prs_opened": len(prs_opened),
        "pr_rate": pct(len(prs_opened)),
        "avg_turnaround": avg_turnaround,
    }

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with Session(engine) as session:
        tasks = session.exec(select(IssueTask)).all()

    metrics = compute_metrics(tasks)
    metric_cards_html = render_metric_cards(metrics)

    rows = ""
    for t in tasks:
        issue_link = f'<a href="{t.issue_url}" target="_blank">#{t.issue_number}</a>' if t.issue_url else f'#{t.issue_number}'
        rows += f"""
        <tr id="row-{t.issue_number}">
            <td>{issue_link}</td>
            <td>{STATUS_DETAIL_LABELS[t.status]}</td>
            <td>{STATUS_DETAIL_LABELS[t.status_detail]}</td>
            <td>{f'<a href="{t.pr_url}" target="_blank">PR for #{t.issue_number}</a>' if t.pr_url else 'N/A'}</td>
            <td>{t.updated.strftime('%Y-%m-%d %H:%M')}</td>
            <td>
                <button onclick="toggleInput({t.issue_number})">💬 Input</button>
                <div id="input-box-{t.issue_number}" style="display:none; margin-top:10px;">
                    <form action="/send_input" method="post">
                        <input type="hidden" name="session_id" value="{t.session_id}">
                        <input type="text" name="user_input" placeholder="Provide input to Devin..." style="width:200px">
                        <button type="submit">Send</button>
                    </form>
                </div>
            </td>
        </tr>
        """
    
    return f"""
    <html>
        <head>
            <title>Devin Automation Dashboard</title>
            <style>
                body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; margin: 40px; background: #f0f2f5; }}
                table {{ width: 100%; border-collapse: collapse; background: white; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }}
                th, td {{ padding: 15px; text-align: left; border-bottom: 1px solid #eee; }}
                th {{ background-color: #2c3e50; color: white; }}
                h1 {{ color: #2c3e50; }}
                .container {{ max-width: 1200px; margin: auto; }}
                .metrics-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
                    gap: 16px;
                    margin-bottom: 30px;
                }}
                .metric-card {{
                    background: white;
                    border-radius: 6px;
                    padding: 18px 20px;
                    box-shadow: 0 2px 5px rgba(0,0,0,0.1);
                }}
                .metric-value {{ font-size: 28px; font-weight: 700; line-height: 1.1; }}
                .metric-label {{ font-size: 13px; color: #555; margin-top: 6px; font-weight: 600; }}
                .metric-sub {{ font-size: 12px; color: #999; margin-top: 2px; }}
            </style>
            <script>
                function toggleInput(id) {{
                    var x = document.getElementById('input-box-' + id);
                    x.style.display = x.style.display === 'none' ? 'block' : 'none';
                }}
            </script>
            <meta http-equiv="refresh" content="30">
        </head>
        <body>
            <div class="container">
                <h1>Devin Autonomous Remediation Dashboard</h1>
                <div class="metrics-grid">
                    {metric_cards_html}
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Issue #</th>
                            <th>Status</th>
                            <th>Status Detail</th>
                            <th>PR URL</th>
                            <th>Last Update</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows if rows else "<tr><td colspan='7'>No issues processed yet.</td></tr>"}
                    </tbody>
                </table>
            </div>
        </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, workers=1)