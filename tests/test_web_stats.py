import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
from starlette.testclient import TestClient

from app.database import Database
from app.models import (
    Project, Job, Record, RecordStage, DecisionOutcome,
    Attachment, AttachmentStatus
)
from app.web import app
import app.web as web_module


def test_record_action_updates_job_stats(tmp_path: Path):
    test_db = Database(tmp_path / "test_stats.db")
    
    # Setup project and job
    project = Project(
        id="proj_stat1",
        name="Stat Test Project",
        source_url="https://example.com",
        instructions="Collect docs",
        output_dir=str(tmp_path / "out"),
        created_at="2026-10-11T00:00:00",
        updated_at="2026-10-11T00:00:00"
    )
    test_db.save_project(project)
    job = test_db.create_job(project.id)
    
    # Create records
    r_uncertain = Record(
        run_id=job.id,
        record_id="REC-001",
        source_url="https://example.com/1",
        decision=DecisionOutcome.UNCERTAIN,
        status="uncertain",
        title="Uncertain Record"
    )
    r_reject = Record(
        run_id=job.id,
        record_id="REC-002",
        source_url="https://example.com/2",
        decision=DecisionOutcome.REJECT,
        status="reject",
        title="Rejected Record"
    )
    r_select = Record(
        run_id=job.id,
        record_id="REC-003",
        source_url="https://example.com/3",
        decision=DecisionOutcome.SELECT,
        status="select",
        title="Selected Record"
    )
    
    r_uncertain = test_db.save_record(r_uncertain)
    r_reject = test_db.save_record(r_reject)
    r_select = test_db.save_record(r_select)
    rec_id1 = r_uncertain.id

    with patch.object(web_module, "db", test_db):
        client = TestClient(app)

        # 1. Initial job details should reconcile initial stats
        res = client.get(f"/api/jobs/{job.id}")
        assert res.status_code == 200
        stats = res.json()["job"]["stats"]
        assert stats["submissions_found"] == 3
        assert stats["submissions_selected"] == 1
        assert stats["submissions_rejected"] == 1
        assert stats["submissions_uncertain"] == 1

        # 2. User manually includes REC-001
        res = client.post(f"/api/records/{rec_id1}/action", json={"action": "include"})
        assert res.status_code == 200
        assert res.json()["new_status"] == "manual_included"

        # Check job stats in DB directly
        updated_job = test_db.get_job(job.id)
        assert updated_job.stats.submissions_selected == 2
        assert updated_job.stats.submissions_uncertain == 0
        assert updated_job.stats.submissions_rejected == 1

        # Check via API
        res = client.get(f"/api/jobs/{job.id}")
        stats = res.json()["job"]["stats"]
        assert stats["submissions_selected"] == 2
        assert stats["submissions_uncertain"] == 0
        assert stats["submissions_rejected"] == 1

        # 3. User manually excludes REC-001
        res = client.post(f"/api/records/{rec_id1}/action", json={"action": "exclude"})
        assert res.status_code == 200
        assert res.json()["new_status"] == "manual_excluded"

        updated_job = test_db.get_job(job.id)
        assert updated_job.stats.submissions_selected == 1
        assert updated_job.stats.submissions_rejected == 2
        assert updated_job.stats.submissions_uncertain == 0


def test_download_approved_updates_stats_and_manifests(tmp_path: Path):
    test_db = Database(tmp_path / "test_download.db")
    out_dir = tmp_path / "out_download"
    out_dir.mkdir(parents=True, exist_ok=True)

    project = Project(
        id="proj_stat2",
        name="Stat Test Download",
        source_url="https://example.com",
        instructions="Collect docs",
        output_dir=str(out_dir),
        created_at="2026-10-11T00:00:00",
        updated_at="2026-10-11T00:00:00"
    )
    test_db.save_project(project)
    job = test_db.create_job(project.id)

    att = Attachment(
        attachment_id="att_101",
        original_name="manual_evidence.pdf",
        url="https://example.com/manual_evidence.pdf",
        download_status=AttachmentStatus.PENDING
    )
    record = Record(
        run_id=job.id,
        record_id="REC-MANUAL-1",
        source_url="https://example.com/item1",
        decision=DecisionOutcome.SELECT,
        status="manual_included",
        title="Manually Included Record",
        attachments=[att]
    )
    test_db.save_record(record)

    # Mock download_attachment on web_module.downloader
    async def mock_download_attachment(attachment, proj, j_id, cancel_event=None):
        attachment.download_status = AttachmentStatus.DOWNLOADED
        attachment.sha256 = "mockhash123456789"
        attachment.local_path = str(out_dir / "manual_evidence.pdf")
        attachment.byte_count = 1024
        test_db.update_attachment(attachment)
        return attachment

    with patch.object(web_module, "db", test_db), \
         patch.object(web_module.downloader, "db", test_db), \
         patch.object(web_module.downloader, "download_attachment", side_effect=mock_download_attachment):
        
        client = TestClient(app)
        res = client.post(f"/api/jobs/{job.id}/download-approved")
        assert res.status_code == 200
        assert res.json()["status"] == "downloading_approved"

        # Starlette TestClient runs background tasks before returning from request
        updated_job = test_db.get_job(job.id)
        assert updated_job.stats.attachments_downloaded == 1
        assert updated_job.stats.unique_files == 1

        # Manifests should exist in run_dir
        run_dir = web_module.downloader.get_run_dir(project, job.id)
        assert (run_dir / "manifest.csv").exists()
        assert (run_dir / "manifest.json").exists()

        # Job details API reflects updated download stats
        res = client.get(f"/api/jobs/{job.id}")
        assert res.status_code == 200
        stats = res.json()["job"]["stats"]
        assert stats["attachments_downloaded"] == 1
        assert stats["unique_files"] == 1
