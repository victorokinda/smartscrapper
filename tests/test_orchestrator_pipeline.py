import asyncio
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from app.database import Database
from app.downloader import Downloader
from app.limiter import RateLimiter
from app.models import (
    Project, Job, JobStatus, Record, RecordStage, DecisionOutcome,
    Attachment, AttachmentStatus, DecisionRubric
)
from app.network import NetworkClient
from app.ollama import OllamaClient
from app.orchestrator import Orchestrator


@pytest.mark.asyncio
async def test_two_stage_decision_pipeline(tmp_path: Path):
    db = Database(tmp_path / "orch.db")
    limiter = RateLimiter(db)
    net = NetworkClient(limiter)
    ollama = OllamaClient(db)
    downloader = Downloader(db, net)
    orchestrator = Orchestrator(db, limiter, net, ollama, downloader)

    project = Project(
        id="orch001",
        name="Test Pipeline",
        source_url="https://mocksite.org/submissions?page=0",
        instructions="Download SB 253 comments",
        output_dir=str(tmp_path / "orch001"),
        created_at="2026-10-11T00:00:00",
        updated_at="2026-10-11T00:00:00"
    )
    job = Job(id=1, project_id=project.id, mode="qwen")
    db.save_project(project)
    job = db.create_job(project.id, mode="qwen")

    listing_html = """
    <table>
      <tr><th>Name</th><th>Subject</th><th>Files</th><th>Comment</th></tr>
      <tr>
        <td>Clear Nonmatch User</td>
        <td>Unrelated topic</td>
        <td>N/A</td>
        <td><a href="/detail/1">View</a></td>
      </tr>
      <tr>
        <td>Needs Detail User</td>
        <td>Ambiguous SB discussion</td>
        <td>N/A</td>
        <td><a href="/detail/2">View</a></td>
      </tr>
    </table>
    """

    detail_html = """
    <article>
      <h1>Ambiguous SB discussion</h1>
      <p>This comment provides detailed input on California SB 253 greenhouse gas reporting.</p>
      <a href="/files/sb253_feedback.pdf">sb253_feedback.pdf</a>
    </article>
    """

    # Mock net.fetch
    visited_urls = []

    async def mock_fetch(url, cancel_event=None):
        visited_urls.append(url)
        resp = MagicMock()
        if "page=0" in url:
            resp.text = listing_html
        elif "detail/2" in url:
            resp.text = detail_html
        else:
            resp.text = "<p>other</p>"
        return resp

    net.fetch = mock_fetch

    # Mock ollama.compile_rubric
    async def mock_compile(inst):
        return DecisionRubric(summary="SB 253 criteria", eligibility_rules=["Must mention SB 253"])

    ollama.compile_rubric = mock_compile

    # Mock ollama.classify
    async def mock_classify(record_data, rubric, stage, backend):
        title = record_data.get("title", "")
        if "Unrelated" in title:
            return DecisionOutcome.REJECT, "Clear nonmatch", None, False
        if stage == RecordStage.LISTING:
            return DecisionOutcome.NEEDS_DETAIL, "Needs full description", None, False
        else:
            return DecisionOutcome.SELECT, "Selected after detail text", None, False

    ollama.classify = mock_classify

    # Mock downloader.download_attachment
    downloaded_files = []

    async def mock_download(att, proj, j_id, cancel_event=None):
        downloaded_files.append(att.original_name)
        att.download_status = AttachmentStatus.DOWNLOADED
        return att

    downloader.download_attachment = mock_download

    # Run pipeline
    cancel_ev = asyncio.Event()
    await orchestrator._run_job_pipeline(project, job, cancel_ev)

    updated_job = db.get_job(job.id)
    assert updated_job.status == JobStatus.COMPLETED

    records = db.get_records_for_job(job.id)
    assert len(records) == 2

    r_nonmatch = next(r for r in records if "Nonmatch" in r.submitter)
    assert r_nonmatch.decision == DecisionOutcome.REJECT
    # CRITICAL: Clear nonmatches MUST NEVER fetch detail pages!
    assert not any("detail/1" in u for u in visited_urls)

    r_detail = next(r for r in records if "Needs Detail" in r.submitter)
    assert r_detail.decision == DecisionOutcome.SELECT
    # Visited detail/2
    assert any("detail/2" in u for u in visited_urls)
    # Downloaded attachment discovered on detail page
    assert "sb253_feedback.pdf" in downloaded_files


@pytest.mark.asyncio
async def test_job_cancellation(tmp_path: Path):
    db = Database(tmp_path / "cancel_orch.db")
    limiter = RateLimiter(db)
    net = NetworkClient(limiter)
    ollama = OllamaClient(db)
    downloader = Downloader(db, net)
    orchestrator = Orchestrator(db, limiter, net, ollama, downloader)

    project = Project(
        id="cancel001",
        name="Cancel Test",
        source_url="https://mocksite.org/listing",
        instructions="Test",
        output_dir=str(tmp_path / "cancel001"),
        created_at="2026-10-11T00:00:00",
        updated_at="2026-10-11T00:00:00"
    )
    db.save_project(project)
    job = db.create_job(project.id)

    async def slow_fetch(url, cancel_event=None):
        while True:
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError()
            await asyncio.sleep(0.05)

    net.fetch = slow_fetch
    ollama.compile_rubric = AsyncMock(return_value=DecisionRubric(summary="test"))

    cancel_ev = asyncio.Event()
    pipeline_task = asyncio.create_task(orchestrator._run_job_pipeline(project, job, cancel_ev))

    await asyncio.sleep(0.1)
    cancel_ev.set()
    await pipeline_task

    updated_job = db.get_job(job.id)
    assert updated_job.status == JobStatus.CANCELLED
