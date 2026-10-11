import asyncio
import os
import time
from pathlib import Path
from app.config import settings
from app.database import Database
from app.downloader import Downloader
from app.limiter import RateLimiter
from app.models import Project, Job, RecordStage, DecisionOutcome, AttachmentStatus
from app.network import NetworkClient
from app.ollama import OllamaClient
from app.parser import HtmlParser


async def run_smoke():
    print("=== STARTING LIVE CARB SMOKE TEST ===")
    test_data_dir = Path("./data/smoke_test")
    test_download_dir = Path("./downloads/smoke_test")
    test_data_dir.mkdir(parents=True, exist_ok=True)
    test_download_dir.mkdir(parents=True, exist_ok=True)

    db = Database(test_data_dir / "smoke.db")
    limiter = RateLimiter(db)
    net = NetworkClient(limiter)
    ollama = OllamaClient(db, base_url="http://127.0.0.1:11434")
    downloader = Downloader(db, net)

    # 1. Project setup
    instructions = (
        "Download comment attachments concerning California SB 253 submitted by industry associations; "
        "institutional investors and financial coalitions; environmental NGOs; environmental justice and community coalitions; "
        "accounting, auditing and professional standards bodies; labor unions and labor coalitions; academic centers; "
        "think tanks; and policy clinics. Include joint SB 253/SB 261 submissions when SB 253 is explicitly covered. "
        "Exclude submissions concerning SB 261 alone, unrelated proceedings, individuals speaking solely in a personal capacity, "
        "and individual commercial companies unless they fall within one of the requested categories. "
        "Use subject, submitter information and attachment filenames as evidence. "
        "Put cases with insufficient evidence into Review rather than guessing."
    )
    project = Project(
        id="carb_smoke",
        name="CARB SB 253 Smoke Test",
        source_url="https://ww2.arb.ca.gov/approved-comments?entity_id=41096&page=0",
        instructions=instructions,
        output_dir=str(test_download_dir.resolve()),
        created_at="2026-10-11T12:00:00",
        updated_at="2026-10-11T12:00:00"
    )
    db.save_project(project)
    job = db.create_job(project.id, mode="decision")
    print(f"Created project {project.id} and job {job.id}")

    # 2. Rubric compilation with Qwen 14B
    print("\n--- Compiling Rubric with Qwen 14B ---")
    start_time = time.time()
    rubric = await ollama.compile_rubric(instructions)
    rubric_time = time.time() - start_time
    print(f"Rubric compiled in {rubric_time:.2f}s:")
    print(f"Summary: {rubric.summary}")
    print(f"Rules: {rubric.eligibility_rules}")
    print(f"Exclusions: {rubric.exclusions}")
    job.rubric = rubric
    db.update_job(job)

    # 3. Model handoff to Tev1 4B
    print("\n--- Model Handoff: Qwen 14B -> Tev1 4B ---")
    handoff_start = time.time()
    await ollama.switch_model_role("classifying", "tev1:4b")
    handoff_time = time.time() - handoff_start
    print(f"Handoff completed in {handoff_time:.2f}s. Resident: {ollama.currently_resident_model}")

    # 4. Fetch live CARB page 0
    print(f"\n--- Fetching live CARB page: {project.source_url} ---")
    fetch_start = time.time()
    resp = await net.fetch(project.source_url)
    print(f"Fetched {len(resp.text)} bytes in {time.time() - fetch_start:.2f}s")

    # 5. Extract records from live page
    records, next_page = HtmlParser.parse_generic_listing(resp.text, project.source_url, 0)
    print(f"Discovered {len(records)} records. Next page: {next_page}")
    assert len(records) > 0, "Expected records from live CARB page!"

    # 6. Classify first 6 sample records with Tev1
    print("\n--- Classifying sample records with Tev1 4B ---")
    sample_records = records[:6]
    for idx, r in enumerate(sample_records):
        r.run_id = job.id
        evidence = {
            "title": r.title,
            "submitter": r.submitter,
            "organization": r.organization,
            "description": r.listing_description,
            "attachments": [a.original_name for a in r.attachments],
            "detail_url": r.detail_url
        }
        t0 = time.time()
        outcome, reason, probs, is_cache = await ollama.classify(
            record_data=evidence,
            rubric=rubric,
            stage=RecordStage.LISTING,
            backend="decision"
        )
        dt = time.time() - t0
        r.decision = outcome
        r.reason = reason
        r.decision_probabilities = probs
        r.status = outcome.value
        r = db.save_record(r)
        print(f"[{idx+1}/{len(sample_records)}] Submitter: {r.submitter[:20]} | Title: {r.title[:30]} -> {outcome.value.upper()} ({reason}) [{dt:.2f}s]")

    # 7. Test download of a selected or sample attachment
    # Find a record with a valid downloadable attachment
    record_with_att = next((r for r in records if r.attachments and "%2522" not in r.attachments[0].url), None)
    if record_with_att:
        att = record_with_att.attachments[0]
        print(f"\n--- Testing document download: {att.original_name} ({att.url}) ---")
        dl_start = time.time()
        downloaded_att = await downloader.download_attachment(att, project, job.id)
        dl_time = time.time() - dl_start
        print(f"Downloaded to {downloaded_att.local_path} ({downloaded_att.byte_count} bytes) in {dl_time:.2f}s")
        print(f"SHA-256: {downloaded_att.sha256}")
        assert os.path.exists(downloaded_att.local_path), "Downloaded file must exist!"
        assert downloaded_att.byte_count > 1000, "Downloaded file must be non-empty document!"

        # Inspect file header to confirm it's a real PDF/document, not HTML error page
        with open(downloaded_att.local_path, "rb") as f:
            header = f.read(16)
            print(f"File magic header: {header}")
            assert not header.lower().startswith(b"<!doctype html"), "File must not be an HTML error page!"

    # 8. Export manifests
    print("\n--- Exporting Manifests ---")
    all_recs = db.get_records_for_job(job.id)
    csv_p, json_p = downloader.export_manifests(project, job, all_recs)
    print(f"Manifest CSV: {csv_p} ({os.path.getsize(csv_p)} bytes)")
    print(f"Manifest JSON: {json_p} ({os.path.getsize(json_p)} bytes)")

    # 9. Clean up residency
    await ollama.unload_model("tev1:4b")
    print("=== LIVE CARB SMOKE TEST COMPLETED SUCCESSFULLY! ===")


if __name__ == "__main__":
    asyncio.run(run_smoke())
