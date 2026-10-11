import asyncio
import json
import pytest
import respx
import httpx
from pathlib import Path
from app.config import settings
from app.database import Database
from app.models import DecisionRubric, DecisionOutcome, RecordStage
from app.ollama import OllamaClient


@pytest.mark.asyncio
async def test_sequential_residency_handoff(tmp_path: Path):
    db = Database(tmp_path / "handoff.db")
    ollama = OllamaClient(db, base_url="http://mock-ollama:11434")

    async with respx.mock(base_url="http://mock-ollama:11434") as respx_mock:
        # Mock /api/generate for unload
        respx_mock.post("/api/generate").respond(200, json={"done": True, "done_reason": "unload"})
        # Mock /api/ps returning empty models list
        respx_mock.get("/api/ps").respond(200, json={"models": []})

        # Set currently resident to qwen3:14b
        ollama.currently_resident_model = "qwen3:14b"
        ollama.current_role = "planning"

        # Switch to classifying with tev1:4b
        await ollama.switch_model_role("classifying", "tev1:4b")
        assert ollama.current_role == "classifying"
        assert ollama.currently_resident_model == "tev1:4b"


@pytest.mark.asyncio
async def test_single_inference_concurrency_lock(tmp_path: Path):
    db = Database(tmp_path / "lock.db")
    ollama = OllamaClient(db, base_url="http://mock-ollama:11434")

    order = []

    async def task_1():
        async with ollama._lock:
            order.append("t1_start")
            await asyncio.sleep(0.1)
            order.append("t1_end")

    async def task_2():
        async with ollama._lock:
            order.append("t2_start")
            order.append("t2_end")

    await asyncio.gather(task_1(), task_2())
    assert order == ["t1_start", "t1_end", "t2_start", "t2_end"]


@pytest.mark.asyncio
async def test_tev1_systemone_response_validation(tmp_path: Path):
    db = Database(tmp_path / "tev1.db")
    ollama = OllamaClient(db, base_url="http://mock-ollama:11434")
    rubric = DecisionRubric(summary="SB 253 comments", eligibility_rules=["Must mention SB 253"])

    async with respx.mock(base_url="http://mock-ollama:11434", assert_all_called=False) as respx_mock:
        respx_mock.post("/api/generate").respond(200, json={"done": True})
        respx_mock.get("/api/ps").respond(200, json={"models": []})
        # Mock systemone endpoint with answers dict
        respx_mock.post("/v1/systemone").respond(
            200,
            json={
                "model": "tev1:4b",
                "answers": {
                    "decision": {
                        "type": "choice",
                        "choice": "select",
                        "probabilities": {"select": 0.88, "reject": 0.12}
                    }
                }
            }
        )

        record_data = {
            "title": "Clean Energy Coalition SB 253",
            "submitter": "Environmental NGO",
            "description": "Public feedback on SB 253",
            "attachments": ["response.pdf"]
        }

        outcome, reason, probs, is_cache = await ollama.classify(
            record_data, rubric, RecordStage.LISTING, backend="decision"
        )
        assert outcome == DecisionOutcome.SELECT
        assert "Selected" in reason
        assert probs["select"] == 0.88
        assert not is_cache

        # Test cache hit on second call
        outcome2, reason2, probs2, is_cache2 = await ollama.classify(
            record_data, rubric, RecordStage.LISTING, backend="decision"
        )
        assert outcome2 == DecisionOutcome.SELECT
        assert is_cache2 is True


@pytest.mark.asyncio
async def test_description_windowing_consensus(tmp_path: Path):
    db = Database(tmp_path / "window.db")
    ollama = OllamaClient(db, base_url="http://mock-ollama:11434")
    rubric = DecisionRubric(summary="SB 253 comments")

    long_text = "This is a very long text. " * 300  # > 5000 chars

    # Test when all windows reject
    async def mock_classify_tev1(rec, rub, stage):
        return DecisionOutcome.REJECT, "Reject", None

    ollama._classify_tev1 = mock_classify_tev1
    outcome, reason, _ = await ollama._classify_tev1_windowed(
        {"description": long_text}, rubric, RecordStage.DETAIL, window_size=2000
    )
    assert outcome == DecisionOutcome.REJECT
    assert "rejected" in reason.lower()
