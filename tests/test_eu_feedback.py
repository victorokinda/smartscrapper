import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.eu_feedback import EUFeedbackPortal
from app.models import RecordStage, AttachmentStatus


def test_is_eu_feedback_url():
    valid_urls = [
        "https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/14861-List-of-non-OECD-countries-that-are-authorized-to-import-certain-non-hazardous-waste-from-the-EU_en",
        "https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/13412-Corporate-sustainability-reporting-directive_en",
        "https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/14861/feedback_en?p_id=26040",
        "https://ec.europa.eu/info/law/better-regulation/brpapi/groupInitiatives/14861",
        "https://ec.europa.eu/info/law/better-regulation/api/allFeedback?publicationId=26040",
    ]
    for u in valid_urls:
        assert EUFeedbackPortal.is_eu_feedback_url(u) is True, f"Failed for {u}"

    invalid_urls = [
        "https://example.com/submissions",
        "https://arb.ca.gov/comments/table",
        "https://gov.uk/consultations",
    ]
    for u in invalid_urls:
        assert EUFeedbackPortal.is_eu_feedback_url(u) is False, f"Failed for {u}"


def test_extract_ids_from_url():
    init_id, pub_id = EUFeedbackPortal.extract_ids_from_url(
        "https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/14861-List-of-non-OECD_en"
    )
    assert init_id == 14861
    assert pub_id is None

    init_id2, pub_id2 = EUFeedbackPortal.extract_ids_from_url(
        "https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/14861/feedback_en?p_id=26040"
    )
    assert init_id2 == 14861
    assert pub_id2 == 26040

    init_id3, pub_id3 = EUFeedbackPortal.extract_ids_from_url(
        "https://ec.europa.eu/info/law/better-regulation/api/allFeedback?publicationId=26040&groupInitiativeId=14861"
    )
    assert init_id3 == 14861
    assert pub_id3 == 26040


@pytest.mark.asyncio
async def test_fetch_feedbacks_with_group_resolution():
    net_client = MagicMock()

    mock_group_response = MagicMock()
    mock_group_response.json.return_value = {
        "shortTitle": "EU Waste Shipments Consultation",
        "publications": [
            {
                "id": 10001,
                "receivingFeedbackStatus": "DISABLED",
                "totalFeedback": 0,
            },
            {
                "id": 26040,
                "receivingFeedbackStatus": "OPEN",
                "totalFeedback": 5,
            }
        ]
    }

    mock_feedback_response = MagicMock()
    mock_feedback_response.json.return_value = {
        "totalPages": 2,
        "totalElements": 15,
        "last": False,
        "content": [
            {
                "id": 9901,
                "firstName": "Jane",
                "surname": "Doe",
                "organization": "Green Future Org",
                "userType": "NGO",
                "country": "BEL",
                "feedback": "This is detailed feedback on sustainability obligations.",
                "attachments": [
                    {
                        "id": 5510,
                        "fileName": "green_future_statement.pdf",
                        "documentId": "0901abcd1234efgh"
                    }
                ]
            },
            {
                "id": 9902,
                "firstName": "John",
                "surname": "Smith",
                "organization": None,
                "userType": "EU_CITIZEN",
                "country": "DEU",
                "feedback": "Individual comment regarding recycling exemptions.",
                "attachments": []
            }
        ]
    }

    async def mock_fetch(url, cancel_event=None):
        if "groupInitiatives" in url:
            return mock_group_response
        elif "allFeedback" in url:
            return mock_feedback_response
        raise ValueError(f"Unexpected url: {url}")

    net_client.fetch = AsyncMock(side_effect=mock_fetch)

    starting_url = "https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/14861-Waste-Directive"
    records, next_page_url, title = await EUFeedbackPortal.fetch_feedbacks(
        starting_url,
        net_client=net_client,
        page_number=0,
        page_size=10
    )

    assert title == "EU Waste Shipments Consultation"
    assert len(records) == 2
    assert next_page_url is not None
    assert "page=1" in next_page_url
    assert "publicationId=26040" in next_page_url

    # Inspect record 1
    r1 = records[0]
    assert r1.record_id == "eu_26040_9901"
    assert r1.submitter == "Jane Doe"
    assert r1.organization == "Green Future Org"
    assert "sustainability obligations" in r1.listing_description
    assert "[Country: BEL]" in r1.listing_description
    assert "[User Type: NGO]" in r1.listing_description
    assert r1.stage == RecordStage.LISTING
    assert len(r1.attachments) == 1

    att = r1.attachments[0]
    assert att.original_name == "green_future_statement.pdf"
    assert att.url == "https://ec.europa.eu/info/law/better-regulation/api/download/0901abcd1234efgh"
    assert att.download_status == AttachmentStatus.PENDING

    # Inspect record 2 (no attachments, anonymous citizen)
    r2 = records[1]
    assert r2.record_id == "eu_26040_9902"
    assert r2.submitter == "John Smith"
    assert r2.organization is None
    assert len(r2.attachments) == 0


@pytest.mark.asyncio
async def test_orchestrator_eu_feedback_pipeline(tmp_path):
    from pathlib import Path
    from app.database import Database
    from app.limiter import RateLimiter
    from app.network import NetworkClient
    from app.ollama import OllamaClient
    from app.downloader import Downloader
    from app.orchestrator import Orchestrator
    from app.models import Project, Job, DecisionRubric, DecisionOutcome

    db = Database(tmp_path / "eu_orch.db")
    limiter = RateLimiter(db)
    net = NetworkClient(limiter)
    ollama = OllamaClient(db)
    downloader = Downloader(db, net)
    orchestrator = Orchestrator(db, limiter, net, ollama, downloader)

    project = Project(
        id="proj_eu_01",
        name="EU Waste Comments",
        source_url="https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/14861-Waste-Directive",
        instructions="Download submissions from NGOs and companies",
        output_dir=str(tmp_path / "proj_eu_01"),
        created_at="2026-10-11T00:00:00",
        updated_at="2026-10-11T00:00:00"
    )
    db.save_project(project)
    job = db.create_job(project.id, mode="qwen")

    # Mock Ollama compile rubric
    ollama.compile_rubric = AsyncMock(return_value=DecisionRubric(
        eligibility_rules=["NGO comments", "Company comments"],
        summary="Collect NGO and corporate feedback"
    ))

    # Mock decision classification: select first record, reject second record
    async def mock_classify(rubric, evidence, mode="decision"):
        if "Green Future Org" in str(evidence):
            return DecisionOutcome.SELECT, "Relevant NGO submission", 0.95
        return DecisionOutcome.REJECT, "Individual citizen non-match", 0.90

    ollama.classify_record = AsyncMock(side_effect=mock_classify)

    # Mock net.fetch for EU APIs
    mock_group_resp = MagicMock()
    mock_group_resp.json.return_value = {
        "shortTitle": "EU Waste Shipment Rules",
        "publications": [{"id": 26040, "receivingFeedbackStatus": "OPEN", "totalFeedback": 2}]
    }

    mock_fb_resp = MagicMock()
    mock_fb_resp.json.return_value = {
        "totalPages": 1,
        "totalElements": 2,
        "last": True,
        "content": [
            {
                "id": 101,
                "firstName": "Jane",
                "surname": "Doe",
                "organization": "Green Future Org",
                "userType": "NGO",
                "feedback": "Submission on green waste rules",
                "attachments": [{"id": 501, "fileName": "green_feedback.pdf", "documentId": "doc501"}]
            },
            {
                "id": 102,
                "firstName": "Bob",
                "surname": "Citizen",
                "organization": None,
                "userType": "EU_CITIZEN",
                "feedback": "Random thought",
                "attachments": []
            }
        ]
    }

    async def mock_fetch(url, cancel_event=None):
        if "groupInitiatives" in url:
            return mock_group_resp
        elif "allFeedback" in url:
            return mock_fb_resp
        raise ValueError(f"Unexpected url {url}")

    net.fetch = AsyncMock(side_effect=mock_fetch)

    # Mock downloader
    async def mock_download_attachment(att, proj, run_id, cancel_event=None):
        att.download_status = AttachmentStatus.DOWNLOADED
        att.local_path = str(Path(proj.output_dir) / "files" / att.original_name)
        att.sha256 = "hash12345"
        att.byte_count = 2048
        db.update_attachment(att)
        return att

    downloader.download_attachment = AsyncMock(side_effect=mock_download_attachment)

    cancel_ev = asyncio.Event()
    await orchestrator._run_job_pipeline(project, job, cancel_ev)

    cur_job = db.get_job(job.id)
    assert cur_job.status.value == "completed"
    assert cur_job.stats.submissions_found == 2
    assert cur_job.stats.submissions_selected == 1
    assert cur_job.stats.submissions_rejected == 1
    assert cur_job.stats.attachments_downloaded == 1

    records = db.get_records_for_job(job.id)
    assert len(records) == 2
    selected = [r for r in records if r.decision == DecisionOutcome.SELECT]
    assert len(selected) == 1
    assert selected[0].record_id == "eu_26040_101"
    assert selected[0].attachments[0].download_status == AttachmentStatus.DOWNLOADED
