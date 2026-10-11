import asyncio
import json
import os
import uuid
import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any, Literal
from fastapi import FastAPI, HTTPException, Request, Response, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.database import Database
from app.downloader import Downloader, sanitize_project_slug, sanitize_filename
from app.limiter import RateLimiter
from app.models import (
    Project, Job, JobStatus, Record, RecordStage, DecisionOutcome,
    AttachmentStatus, RunStats, RunEvent
)
from app.network import NetworkClient
from app.ollama import OllamaClient
from app.orchestrator import Orchestrator


app = FastAPI(title="SmartScraper", description="Local Smart Document Scraper")

# Initialize singletons
db = Database()
rate_limiter = RateLimiter(db)
net_client = NetworkClient(rate_limiter)
ollama_client = OllamaClient(db)
downloader = Downloader(db, net_client)
orchestrator = Orchestrator(db, rate_limiter, net_client, ollama_client, downloader)


# Request schemas
class StartJobRequest(BaseModel):
    project_name: str
    source_url: str
    instructions: str
    mode: Literal["decision", "qwen"] = "decision"
    project_id: Optional[str] = None


class RecordActionRequest(BaseModel):
    action: Literal["include", "exclude"]


# Static files
static_dir = Path(__file__).resolve().parent.parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = static_dir / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h3>SmartScraper frontend loading...</h3>")


@app.get("/api/status")
async def get_system_status():
    loaded_models = await ollama_client.get_loaded_models()
    limiter_status = rate_limiter.get_status()
    active_job_id = None
    for j_id, task in orchestrator.active_jobs.items():
        if not task.done():
            active_job_id = j_id
            break

    active_job = db.get_job(active_job_id) if active_job_id else None

    return {
        "ollama_base_url": settings.OLLAMA_BASE_URL,
        "planner_model": settings.PLANNER_MODEL,
        "classifier_model": settings.CLASSIFIER_MODEL,
        "qwen_only_model": settings.QWEN_ONLY_MODEL,
        "active_backend": settings.CLASSIFIER_BACKEND,
        "model_residency": settings.MODEL_RESIDENCY,
        "current_role": ollama_client.current_role,
        "resident_model": ollama_client.currently_resident_model,
        "loaded_models": loaded_models,
        "limiter": limiter_status,
        "active_job": active_job.model_dump() if active_job else None
    }


@app.get("/api/projects")
async def list_projects():
    return [p.model_dump() for p in db.list_projects()]


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    p = db.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    latest_job = db.get_latest_job_for_project(project_id)
    return {
        "project": p.model_dump(),
        "latest_job": latest_job.model_dump() if latest_job else None
    }


@app.post("/api/jobs/start")
async def start_job(req: StartJobRequest):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    # Check if project exists by ID or by exact name
    project = None
    if req.project_id:
        project = db.get_project(req.project_id)
    if not project:
        # Search by name or create new
        existing_list = db.list_projects()
        for p in existing_list:
            if p.name.strip().lower() == req.project_name.strip().lower():
                project = p
                break

    if not project:
        proj_id = str(uuid.uuid4())[:8]
        slug = sanitize_project_slug(req.project_name, proj_id)
        out_dir = str((settings.download_path / slug).resolve())
        project = Project(
            id=proj_id,
            name=req.project_name.strip(),
            source_url=req.source_url.strip(),
            instructions=req.instructions.strip(),
            output_dir=out_dir,
            created_at=now,
            updated_at=now
        )
    else:
        project.source_url = req.source_url.strip()
        project.instructions = req.instructions.strip()
        project.updated_at = now

    db.save_project(project)
    # Ensure project output directory exists
    Path(project.output_dir).mkdir(parents=True, exist_ok=True)

    job = db.create_job(project.id, mode=req.mode)
    await orchestrator.start_job(project, job)
    return {"job": job.model_dump(), "project": project.model_dump()}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: int):
    await orchestrator.cancel_job(job_id)
    return {"status": "cancelled", "job_id": job_id}


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    project = db.get_project(job.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await orchestrator.resume_job(project, job)
    return {"status": "resumed", "job_id": job.id}


@app.get("/api/jobs/{job_id}")
async def get_job_details(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    events = db.get_events(job_id, limit=50)
    records = db.get_records_for_job(job_id)
    return {
        "job": job.model_dump(),
        "events": [e.model_dump() for e in events],
        "records": [r.model_dump() for r in records]
    }


@app.post("/api/records/{record_id}/action")
async def record_action(record_id: int, req: RecordActionRequest):
    # Retrieve record from DB
    with db._get_conn() as conn:
        row = conn.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Record not found")
        job_id = row["job_id"]
        job = db.get_job(job_id)

    if req.action == "include":
        new_status = "manual_included"
        new_decision = DecisionOutcome.SELECT.value
        reason = "Manually approved by user"
    else:
        new_status = "manual_excluded"
        new_decision = DecisionOutcome.REJECT.value
        reason = "Manually excluded by user"

    with db._get_conn() as conn:
        conn.execute("""
        UPDATE records SET
            status = ?,
            decision = ?,
            reason = ?
        WHERE id = ?
        """, (new_status, new_decision, reason, record_id))
        conn.commit()

    db.log_event(job_id, f"Record {row['record_id']} {req.action}d by user", level="info")
    return {"status": "success", "new_status": new_status}


@app.post("/api/jobs/{job_id}/download-approved")
async def download_approved(job_id: int, bg_tasks: BackgroundTasks):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    project = db.get_project(job.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    records = db.get_records_for_job(job_id)
    approved_records = [r for r in records if r.status in ("manual_included", "select")]

    async def _download_task():
        for r in approved_records:
            # If no attachments but detail URL exists, visit detail page first
            if not r.attachments and r.detail_url:
                try:
                    d_resp = await net_client.fetch(r.detail_url)
                    _, d_atts = HtmlParser.parse_generic_detail(d_resp.text, r.detail_url)
                    r.attachments.extend(d_atts)
                    db.save_record(r)
                except Exception as e:
                    db.log_event(job_id, f"Failed detail fetch for manual item {r.record_id}: {e}", level="warning")

            for att in r.attachments:
                if att.download_status not in (AttachmentStatus.DOWNLOADED, AttachmentStatus.REUSED):
                    try:
                        await downloader.download_attachment(att, project, job_id)
                        db.log_event(job_id, f"Downloaded approved file {att.original_name}", level="success")
                    except Exception as e:
                        db.log_event(job_id, f"Failed approved file download {att.original_name}: {e}", level="warning")

    bg_tasks.add_task(_download_task)
    return {"status": "downloading_approved"}


@app.get("/api/jobs/{job_id}/manifest.csv")
async def get_manifest_csv(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    project = db.get_project(job.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    records = db.get_records_for_job(job_id)
    csv_path, _ = downloader.export_manifests(project, job, records)
    return FileResponse(csv_path, media_type="text/csv", filename=f"manifest_job_{job_id}.csv")


@app.get("/api/jobs/{job_id}/manifest.json")
async def get_manifest_json(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    project = db.get_project(job.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    records = db.get_records_for_job(job_id)
    _, json_path = downloader.export_manifests(project, job, records)
    return FileResponse(json_path, media_type="application/json", filename=f"manifest_job_{job_id}.json")


@app.get("/api/files/{project_id}/{filename}")
async def get_downloaded_file(project_id: str, filename: str):
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    safe_name = sanitize_filename(filename)
    file_path = downloader.get_project_dir(project) / "files" / safe_name
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, filename=safe_name)


@app.get("/api/events")
async def event_stream(request: Request):
    async def sse():
        while True:
            if await request.is_disconnected():
                break
            status_data = await get_system_status()
            yield f"data: {json.dumps(status_data)}\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(sse(), media_type="text/event-stream")
