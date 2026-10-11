import csv
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
import httpx
from app.config import settings
from app.database import Database
from app.limiter import RateLimiter
from app.models import Attachment, AttachmentStatus, Record, Job, Project
from app.network import NetworkClient, validate_public_url


def sanitize_filename(name: str) -> str:
    # Normalize backslashes for cross-platform path safety
    name = name.replace("\\", "/")
    base = os.path.basename(name)
    clean = re.sub(r'[^a-zA-Z0-9_\-\. ]', '_', base)
    clean = clean.strip()
    return clean or "document.bin"


def sanitize_project_slug(name: str, proj_id: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9_\-]', '-', name.lower()).strip('-')
    if not slug:
        slug = "project"
    short_id = proj_id[:8]
    return f"{slug}--{short_id}"


class Downloader:
    def __init__(self, db: Database, net_client: NetworkClient):
        self.db = db
        self.net = net_client

    def get_project_dir(self, project: Project) -> Path:
        slug = sanitize_project_slug(project.name, project.id)
        p_dir = settings.download_path / slug
        (p_dir / "files").mkdir(parents=True, exist_ok=True)
        return p_dir

    def get_run_dir(self, project: Project, run_id: int) -> Path:
        p_dir = self.get_project_dir(project)
        r_dir = p_dir / "runs" / str(run_id)
        r_dir.mkdir(parents=True, exist_ok=True)
        return r_dir

    async def download_attachment(
        self,
        att: Attachment,
        project: Project,
        run_id: int,
        cancel_event: Optional[Any] = None
    ) -> Attachment:
        validate_public_url(att.url)
        files_dir = self.get_project_dir(project) / "files"

        # Check URL deduplication across this project's existing files
        records = self.db.get_records_for_job(run_id)
        for r in records:
            for existing in r.attachments:
                if existing.url == att.url and existing.download_status in (AttachmentStatus.DOWNLOADED, AttachmentStatus.REUSED) and existing.local_path:
                    if os.path.exists(existing.local_path):
                        att.local_path = existing.local_path
                        att.sha256 = existing.sha256
                        att.byte_count = existing.byte_count
                        att.download_status = AttachmentStatus.REUSED
                        att.is_reused = True
                        self.db.update_attachment(att)
                        return att

        # Sanitize filename
        safe_name = sanitize_filename(att.original_name)
        # Avoid overwriting
        dest_path = files_dir / safe_name
        counter = 1
        while dest_path.exists():
            stem = dest_path.stem
            suffix = dest_path.suffix
            dest_path = files_dir / f"{stem}_{counter}{suffix}"
            counter += 1

        part_path = dest_path.with_suffix(dest_path.suffix + ".part")

        # Download via network client / rate limiter with retries
        origin = self.net.limiter.origins.get(att.url)
        max_retries = settings.MAX_HTTP_RETRIES
        attempt = 0
        last_error = None

        while attempt <= max_retries:
            attempt += 1
            if cancel_event and cancel_event.is_set():
                att.download_status = AttachmentStatus.FAILED
                att.error = "Download cancelled"
                self.db.update_attachment(att)
                return att

            sem, orig_lim = await self.net.limiter.acquire(att.url, cancel_event)
            try:
                headers = {"User-Agent": self.net.user_agent}
                max_bytes = settings.MAX_FILE_MB * 1024 * 1024
                sha256_hasher = hashlib.sha256()
                total_bytes = 0

                async with httpx.AsyncClient(timeout=settings.HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
                    async with client.stream("GET", att.url, headers=headers) as resp:
                        if resp.status_code in (429, 503):
                            retry_after = resp.headers.get("Retry-After")
                            cooldown = self.net.limiter.record_throttle(orig_lim.origin, retry_after)
                            last_error = f"HTTP {resp.status_code}"
                            if attempt <= max_retries:
                                await asyncio.sleep(min(cooldown, 30.0))
                                continue
                            else:
                                resp.raise_for_status()

                        resp.raise_for_status()

                        # Check content type and first chunk for HTML error page
                        first_chunk = True
                        with open(part_path, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=16384):
                                if cancel_event and cancel_event.is_set():
                                    raise httpx.RequestError("Download cancelled")

                                if first_chunk:
                                    first_chunk = False
                                    lower_head = chunk[:1024].lower()
                                    if b"<!doctype html" in lower_head or b"<html" in lower_head:
                                        if not att.original_name.lower().endswith((".html", ".htm")):
                                            raise ValueError("Download response is an HTML page rather than requested document")

                                total_bytes += len(chunk)
                                if total_bytes > max_bytes:
                                    raise ValueError(f"File exceeded size limit of {settings.MAX_FILE_MB}MB")

                                sha256_hasher.update(chunk)
                                f.write(chunk)

                # Atomic rename
                os.replace(part_path, dest_path)
                file_hash = sha256_hasher.hexdigest()

                att.local_path = str(dest_path)
                att.sha256 = file_hash
                att.byte_count = total_bytes
                att.download_status = AttachmentStatus.DOWNLOADED
                att.is_reused = False
                att.error = None
                self.db.update_attachment(att)
                self.net.limiter.record_success(orig_lim.origin)
                return att

            except Exception as e:
                last_error = str(e)
                if part_path.exists():
                    try:
                        part_path.unlink()
                    except Exception:
                        pass
                if attempt > max_retries:
                    att.download_status = AttachmentStatus.FAILED
                    att.error = last_error
                    self.db.update_attachment(att)
                    return att
                await asyncio.sleep(2.0 * attempt)
            finally:
                self.net.limiter.release(orig_lim)

        att.download_status = AttachmentStatus.FAILED
        att.error = last_error or "Download failed"
        self.db.update_attachment(att)
        return att

    def export_manifests(self, project: Project, job: Job, records: List[Record]) -> Tuple[Path, Path]:
        run_dir = self.get_run_dir(project, job.id)
        csv_path = run_dir / "manifest.csv"
        json_path = run_dir / "manifest.json"

        # Export CSV
        headers = [
            "record_id", "page_number", "title", "submitter", "organization", "proceeding",
            "source_url", "detail_url", "stage", "decision", "reason",
            "decision_backend", "decision_model", "attachment_id", "original_name",
            "attachment_url", "download_status", "is_reused", "local_path", "sha256", "byte_count", "error"
        ]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            for r in records:
                if not r.attachments:
                    writer.writerow([
                        r.record_id, r.page_number, r.title, r.submitter, r.organization or "", r.proceeding or "",
                        r.source_url, r.detail_url or "", r.stage.value, r.decision.value if r.decision else "", r.reason or "",
                        r.decision_backend or "", r.decision_model or "", "", "", "", "no_attachment", 0, "", "", "", ""
                    ])
                else:
                    for att in r.attachments:
                        writer.writerow([
                            r.record_id, r.page_number, r.title, r.submitter, r.organization or "", r.proceeding or "",
                            r.source_url, r.detail_url or "", r.stage.value, r.decision.value if r.decision else "", r.reason or "",
                            r.decision_backend or "", r.decision_model or "", att.attachment_id, att.original_name,
                            att.url, att.download_status.value, 1 if att.is_reused else 0, att.local_path or "",
                            att.sha256 or "", att.byte_count or 0, att.error or ""
                        ])

        # Export JSON
        manifest_data = {
            "project": project.model_dump(),
            "job": job.model_dump(),
            "records": [r.model_dump() for r in records]
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)

        return csv_path, json_path
