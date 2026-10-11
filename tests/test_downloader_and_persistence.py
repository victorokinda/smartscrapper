import os
import json
import csv
import pytest
from pathlib import Path
from app.database import Database
from app.downloader import Downloader, sanitize_filename, sanitize_project_slug
from app.limiter import RateLimiter
from app.network import NetworkClient
from app.models import Project, Job, Record, Attachment, AttachmentStatus, RecordStage, DecisionOutcome


def test_sanitize_filename_and_path_safety():
    assert sanitize_filename("../../../etc/passwd") == "passwd"
    assert sanitize_filename("..\\..\\windows\\system32.dll") == "system32.dll"
    assert sanitize_filename("Normal Report.pdf") == "Normal Report.pdf"
    assert sanitize_filename("Special & Characters #1.docx") == "Special _ Characters _1.docx"


def test_sanitize_project_slug():
    assert sanitize_project_slug("CARB Comments / 2026", "a1b2c3d4") == "carb-comments---2026--a1b2c3d4"
    assert sanitize_project_slug("../../../hacked", "deadbeef") == "hacked--deadbeef"


def test_project_name_collision_different_ids(tmp_path: Path):
    db = Database(tmp_path / "coll.db")
    p1 = Project(
        id="proj0001",
        name="EU CSRD",
        source_url="https://example.com/1",
        instructions="Instructions",
        output_dir=str(tmp_path / "eu-csrd--proj0001"),
        created_at="2026-10-11T00:00:00",
        updated_at="2026-10-11T00:00:00"
    )
    p2 = Project(
        id="proj0002",
        name="EU CSRD",
        source_url="https://example.com/2",
        instructions="Instructions 2",
        output_dir=str(tmp_path / "eu-csrd--proj0002"),
        created_at="2026-10-11T00:00:00",
        updated_at="2026-10-11T00:00:00"
    )
    db.save_project(p1)
    db.save_project(p2)

    projects = db.list_projects()
    assert len(projects) == 2
    assert p1.output_dir != p2.output_dir


def test_manifest_export_csv_and_json(tmp_path: Path):
    db = Database(tmp_path / "manifest.db")
    limiter = RateLimiter(db)
    net = NetworkClient(limiter)
    downloader = Downloader(db, net)

    project = Project(
        id="p1234567",
        name="Test Manifest",
        source_url="https://example.com",
        instructions="Instructions",
        output_dir=str(tmp_path / "test-manifest--p1234567"),
        created_at="2026-10-11T00:00:00",
        updated_at="2026-10-11T00:00:00"
    )
    job = Job(id=1, project_id=project.id, mode="decision")

    rec1 = Record(
        record_id="rec_1",
        source_url="https://example.com",
        page_number=0,
        title="Title 1",
        submitter="Org A",
        decision=DecisionOutcome.SELECT,
        reason="Matches criteria",
        attachments=[
            Attachment(
                attachment_id="att_1",
                original_name="report.pdf",
                url="https://example.com/files/report.pdf",
                download_status=AttachmentStatus.DOWNLOADED,
                local_path=str(tmp_path / "report.pdf"),
                sha256="abc123sha",
                byte_count=1024
            )
        ]
    )
    # Record without attachment
    rec2 = Record(
        record_id="rec_2",
        source_url="https://example.com",
        page_number=0,
        title="Title 2",
        submitter="Org B",
        decision=DecisionOutcome.REJECT,
        reason="Nonmatch",
        attachments=[]
    )

    csv_path, json_path = downloader.export_manifests(project, job, [rec1, rec2])
    assert csv_path.exists()
    assert json_path.exists()

    # Verify CSV content
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = list(csv.reader(f))
        assert len(reader) == 3  # header + 2 records
        header = reader[0]
        assert "record_id" in header
        assert "sha256" in header
        row1 = reader[1]
        assert row1[0] == "rec_1"
        assert row1[9] == "select"
        row2 = reader[2]
        assert row2[0] == "rec_2"
        assert "no_attachment" in row2

    # Verify JSON content
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["job"]["id"] == 1
        assert len(data["records"]) == 2
