"""
Standalone simulation script that walks through the full remediation
lifecycle end-to-end — trigger, in-progress, completed with a PR — without
making any real GitHub or Devin API calls.
"""
import asyncio
import os
from datetime import datetime, timezone

os.environ.setdefault("DEVIN_ORG_ID", "sim-org")
os.environ.setdefault("DEVIN_API_KEY", "sim-key")
os.environ.setdefault("GITHUB_REPO", "")  # keep github_poller's network path inert

import main
from sqlmodel import SQLModel, create_engine, Session, select  # noqa: E402

SIM_ISSUE_NUMBER = 9001
SIM_REPO_URL = "https://github.com/your-org/superset"


async def fake_create_session(payload):
    print(f"  [mock devin] create_session called for repo={payload['repository']}")
    return {"session_id": "sim-session-001", "status": "new", "status_detail": None}


async def fake_get_session(devin_id):
    print(f"  [mock devin] get_session called for {devin_id}")
    return {
        "status": "complete",
        "status_detail": "finished",
        "pull_requests": [{"pr_url": f"{SIM_REPO_URL}/pull/42"}],
    }


async def run_simulation():
    print("🚀 Starting Devin Automation Simulation (fully mocked, no network calls)\n")

    # Test DB for simulation use
    main.engine = create_engine("sqlite:///./simulation.db", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(main.engine)

    main.create_session = fake_create_session
    main.get_session = fake_get_session

    print("Step 1: Simulating a new 'devin-fix' labeled issue being picked up by github_poller...")
    await main.process_issue_remediation(
        issue_number=SIM_ISSUE_NUMBER,
        title="Fix dependency vulnerability in requests",
        body="The current version of requests has a known vulnerability. Please upgrade to 2.31.0",
        repo_url=SIM_REPO_URL,
        issue_url=f"{SIM_REPO_URL}/issues/{SIM_ISSUE_NUMBER}",
    )

    with Session(main.engine) as session:
        task = session.exec(
            select(main.IssueTask).where(main.IssueTask.issue_number == SIM_ISSUE_NUMBER)
        ).first()
        assert task is not None, "Task was not created!"
        assert task.session_id == "sim-session-001"
        print(f"  Task created -> status={task.status}, session_id={task.session_id}")

    print("\nStep 2: Simulating status_poller picking up Devin's completion...")
    with Session(main.engine) as session:
        task = session.exec(
            select(main.IssueTask).where(main.IssueTask.issue_number == SIM_ISSUE_NUMBER)
        ).first()
        status_data = await main.get_session(task.session_id)

        # Mirrors the update logic inside status_poller()
        task.status = main.IssueBaseStatus(status_data["status"])
        task.status_detail = main.IssueStatusDetail(status_data["status_detail"])
        all_prs = status_data.get("pull_requests")
        if all_prs:
            task.pr_url = all_prs[-1]["pr_url"]
            task.status = main.IssueBaseStatus.COMPLETE
            task.status_detail = main.IssueStatusDetail.FINISHED
        task.updated = datetime.now(timezone.utc)
        session.add(task)
        session.commit()
        print(f"  Task updated -> status={task.status}, pr_url={task.pr_url}")

    print("\nStep 3: Rendering the dashboard with this task...")
    html = await main.dashboard()
    assert f"#{SIM_ISSUE_NUMBER}" in html
    assert task.pr_url in html
    print("  Dashboard rendered successfully and contains the completed task ✅")

    print("\n✅ Simulation successful — full lifecycle verified:")
    print("   trigger -> Devin session created -> status polled -> PR linked -> shown on dashboard")
    print("\n(A throwaway simulation.db was created in this directory — safe to delete.)")


if __name__ == "__main__":
    asyncio.run(run_simulation())