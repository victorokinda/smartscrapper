import json
import sqlite3
import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
from app.config import settings
from app.models import (
    Project, Job, JobStatus, Record, Attachment, AttachmentStatus,
    RecordStage, DecisionOutcome, RunStats, RunEvent, ExtractionPlan, DecisionRubric
)


def get_db_path() -> Path:
    return settings.db_path


def init_db(db_path: Optional[Path] = None):
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            instructions TEXT NOT NULL,
            output_dir TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id),
            status TEXT NOT NULL,
            mode TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error TEXT,
            stats_json TEXT,
            rubric_json TEXT,
            current_stage TEXT
        )
        """)
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            url TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            status TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            raw_html_hash TEXT
        )
        """)
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS extraction_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            template_signature TEXT UNIQUE NOT NULL,
            plan_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            record_id TEXT NOT NULL,
            source_url TEXT NOT NULL,
            page_number INTEGER NOT NULL,
            title TEXT,
            submitter TEXT,
            organization TEXT,
            proceeding TEXT,
            listing_description TEXT,
            detail_url TEXT,
            detail_description TEXT,
            stage TEXT NOT NULL,
            decision TEXT,
            reason TEXT,
            decision_backend TEXT,
            decision_model TEXT,
            decision_probabilities TEXT,
            status TEXT NOT NULL,
            evidence_used TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(job_id, record_id)
        )
        """)
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            record_id INTEGER NOT NULL REFERENCES records(id),
            attachment_id TEXT NOT NULL,
            original_name TEXT NOT NULL,
            url TEXT NOT NULL,
            local_path TEXT,
            sha256 TEXT,
            byte_count INTEGER,
            download_status TEXT NOT NULL,
            is_reused INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            UNIQUE(job_id, record_id, attachment_id)
        )
        """)
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS decisions_cache (
            cache_key TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            rubric_hash TEXT NOT NULL,
            evidence_hash TEXT NOT NULL,
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT,
            probabilities_json TEXT,
            created_at TEXT NOT NULL
        )
        """)
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS limiter_cooldowns (
            origin TEXT PRIMARY KEY,
            cooldown_until REAL NOT NULL
        )
        """)
        
        conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            timestamp TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            event_type TEXT NOT NULL
        )
        """)
        conn.commit()


class Database:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or get_db_path()
        init_db(self.db_path)

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # Projects
    def save_project(self, project: Project) -> Project:
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO projects (id, name, source_url, instructions, output_dir, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                source_url=excluded.source_url,
                instructions=excluded.instructions,
                output_dir=excluded.output_dir,
                updated_at=excluded.updated_at
            """, (
                project.id, project.name, project.source_url,
                project.instructions, project.output_dir,
                project.created_at, project.updated_at
            ))
            conn.commit()
        return project

    def get_project(self, project_id: str) -> Optional[Project]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            if not row:
                return None
            return Project(
                id=row["id"],
                name=row["name"],
                source_url=row["source_url"],
                instructions=row["instructions"],
                output_dir=row["output_dir"],
                created_at=row["created_at"],
                updated_at=row["updated_at"]
            )

    def list_projects(self) -> List[Project]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
            return [
                Project(
                    id=row["id"],
                    name=row["name"],
                    source_url=row["source_url"],
                    instructions=row["instructions"],
                    output_dir=row["output_dir"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"]
                )
                for row in rows
            ]

    # Jobs
    def create_job(self, project_id: str, mode: str = "decision") -> Job:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_conn() as conn:
            cur = conn.execute("""
            INSERT INTO jobs (project_id, status, mode, started_at, stats_json, rubric_json, current_stage)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                project_id, JobStatus.IDLE.value, mode, now,
                json.dumps(RunStats().model_dump()), None, "idle"
            ))
            job_id = cur.lastrowid
            conn.commit()
        return self.get_job(job_id)

    def update_job(self, job: Job):
        with self._get_conn() as conn:
            conn.execute("""
            UPDATE jobs SET
                status = ?,
                mode = ?,
                started_at = ?,
                finished_at = ?,
                error = ?,
                stats_json = ?,
                rubric_json = ?,
                current_stage = ?
            WHERE id = ?
            """, (
                job.status.value,
                job.mode,
                job.started_at,
                job.finished_at,
                job.error,
                json.dumps(job.stats.model_dump()),
                json.dumps(job.rubric.model_dump()) if job.rubric else None,
                job.current_stage,
                job.id
            ))
            conn.commit()

    def get_job(self, job_id: int) -> Optional[Job]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if not row:
                return None
            stats = RunStats(**json.loads(row["stats_json"])) if row["stats_json"] else RunStats()
            rubric = DecisionRubric(**json.loads(row["rubric_json"])) if row["rubric_json"] else None
            return Job(
                id=row["id"],
                project_id=row["project_id"],
                status=JobStatus(row["status"]),
                mode=row["mode"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                error=row["error"],
                stats=stats,
                rubric=rubric,
                current_stage=row["current_stage"] or "idle"
            )

    def get_latest_job_for_project(self, project_id: str) -> Optional[Job]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE project_id = ? ORDER BY id DESC LIMIT 1",
                (project_id,)
            ).fetchone()
            if not row:
                return None
            return self.get_job(row["id"])

    # Records and Attachments
    def save_record(self, record: Record) -> Record:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_conn() as conn:
            cur = conn.execute("""
            INSERT INTO records (
                job_id, record_id, source_url, page_number, title, submitter,
                organization, proceeding, listing_description, detail_url,
                detail_description, stage, decision, reason, decision_backend,
                decision_model, decision_probabilities, status, evidence_used, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, record_id) DO UPDATE SET
                title=excluded.title,
                submitter=excluded.submitter,
                organization=excluded.organization,
                proceeding=excluded.proceeding,
                listing_description=excluded.listing_description,
                detail_url=excluded.detail_url,
                detail_description=excluded.detail_description,
                stage=excluded.stage,
                decision=excluded.decision,
                reason=excluded.reason,
                decision_backend=excluded.decision_backend,
                decision_model=excluded.decision_model,
                decision_probabilities=excluded.decision_probabilities,
                status=excluded.status,
                evidence_used=excluded.evidence_used
            """, (
                record.run_id, record.record_id, record.source_url, record.page_number,
                record.title, record.submitter, record.organization, record.proceeding,
                record.listing_description, record.detail_url, record.detail_description,
                record.stage.value, record.decision.value if record.decision else None,
                record.reason, record.decision_backend, record.decision_model,
                json.dumps(record.decision_probabilities) if record.decision_probabilities else None,
                record.status, json.dumps(record.evidence_used) if record.evidence_used else None,
                now
            ))
            if cur.lastrowid:
                record.id = cur.lastrowid
            elif not record.id:
                row = conn.execute(
                    "SELECT id FROM records WHERE job_id = ? AND record_id = ?",
                    (record.run_id, record.record_id)
                ).fetchone()
                if row:
                    record.id = row["id"]

            # Save attachments
            for att in record.attachments:
                att.run_id = record.run_id
                att.record_id = record.id
                conn.execute("""
                INSERT INTO attachments (
                    job_id, record_id, attachment_id, original_name, url,
                    local_path, sha256, byte_count, download_status, is_reused, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, record_id, attachment_id) DO UPDATE SET
                    original_name=excluded.original_name,
                    url=excluded.url,
                    local_path=excluded.local_path,
                    sha256=excluded.sha256,
                    byte_count=excluded.byte_count,
                    download_status=excluded.download_status,
                    is_reused=excluded.is_reused,
                    error=excluded.error
                """, (
                    att.run_id, att.record_id, att.attachment_id, att.original_name,
                    att.url, att.local_path, att.sha256, att.byte_count,
                    att.download_status.value, 1 if att.is_reused else 0, att.error
                ))
            conn.commit()
        return record

    def get_records_for_job(self, job_id: int) -> List[Record]:
        with self._get_conn() as conn:
            r_rows = conn.execute("SELECT * FROM records WHERE job_id = ? ORDER BY id ASC", (job_id,)).fetchall()
            a_rows = conn.execute("SELECT * FROM attachments WHERE job_id = ? ORDER BY id ASC", (job_id,)).fetchall()
            
            att_by_rec: Dict[int, List[Attachment]] = {}
            for ar in a_rows:
                att = Attachment(
                    id=ar["id"],
                    record_id=ar["record_id"],
                    run_id=ar["job_id"],
                    attachment_id=ar["attachment_id"],
                    original_name=ar["original_name"],
                    url=ar["url"],
                    local_path=ar["local_path"],
                    sha256=ar["sha256"],
                    byte_count=ar["byte_count"],
                    download_status=AttachmentStatus(ar["download_status"]),
                    is_reused=bool(ar["is_reused"]),
                    error=ar["error"]
                )
                att_by_rec.setdefault(ar["record_id"], []).append(att)

            records = []
            for rr in r_rows:
                probs = json.loads(rr["decision_probabilities"]) if rr["decision_probabilities"] else None
                evidence = json.loads(rr["evidence_used"]) if rr["evidence_used"] else None
                records.append(Record(
                    id=rr["id"],
                    run_id=rr["job_id"],
                    record_id=rr["record_id"],
                    source_url=rr["source_url"],
                    page_number=rr["page_number"],
                    title=rr["title"] or "",
                    submitter=rr["submitter"] or "",
                    organization=rr["organization"],
                    proceeding=rr["proceeding"],
                    listing_description=rr["listing_description"],
                    detail_url=rr["detail_url"],
                    detail_description=rr["detail_description"],
                    stage=RecordStage(rr["stage"]),
                    decision=DecisionOutcome(rr["decision"]) if rr["decision"] else None,
                    reason=rr["reason"],
                    decision_backend=rr["decision_backend"],
                    decision_model=rr["decision_model"],
                    decision_probabilities=probs,
                    status=rr["status"],
                    evidence_used=evidence,
                    attachments=att_by_rec.get(rr["id"], [])
                ))
            return records

    def update_attachment(self, att: Attachment):
        with self._get_conn() as conn:
            conn.execute("""
            UPDATE attachments SET
                local_path = ?,
                sha256 = ?,
                byte_count = ?,
                download_status = ?,
                is_reused = ?,
                error = ?
            WHERE job_id = ? AND record_id = ? AND attachment_id = ?
            """, (
                att.local_path, att.sha256, att.byte_count,
                att.download_status.value, 1 if att.is_reused else 0,
                att.error, att.run_id, att.record_id, att.attachment_id
            ))
            conn.commit()

    # Decisions Cache
    def get_cached_decision(self, cache_key: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM decisions_cache WHERE cache_key = ?", (cache_key,)).fetchone()
            if not row:
                return None
            return {
                "decision": row["decision"],
                "reason": row["reason"],
                "probabilities": json.loads(row["probabilities_json"]) if row["probabilities_json"] else None
            }

    def set_cached_decision(
        self, cache_key: str, stage: str, rubric_hash: str, evidence_hash: str,
        backend: str, model: str, decision: str, reason: Optional[str],
        probabilities: Optional[Dict[str, float]]
    ):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO decisions_cache (
                cache_key, stage, rubric_hash, evidence_hash, backend, model,
                decision, reason, probabilities_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                decision=excluded.decision,
                reason=excluded.reason,
                probabilities_json=excluded.probabilities_json
            """, (
                cache_key, stage, rubric_hash, evidence_hash, backend, model,
                decision, reason, json.dumps(probabilities) if probabilities else None,
                now
            ))
            conn.commit()

    # Limiter cooldowns
    def get_origin_cooldown(self, origin: str) -> Optional[float]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT cooldown_until FROM limiter_cooldowns WHERE origin = ?", (origin,)).fetchone()
            if not row:
                return None
            return float(row["cooldown_until"])

    def set_origin_cooldown(self, origin: str, cooldown_until: float):
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO limiter_cooldowns (origin, cooldown_until) VALUES (?, ?)
            ON CONFLICT(origin) DO UPDATE SET cooldown_until=excluded.cooldown_until
            """, (origin, cooldown_until))
            conn.commit()

    def clear_origin_cooldown(self, origin: str):
        with self._get_conn() as conn:
            conn.execute("DELETE FROM limiter_cooldowns WHERE origin = ?", (origin,))
            conn.commit()

    # Extraction plans cache
    def get_extraction_plan(self, template_signature: str) -> Optional[ExtractionPlan]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT plan_json FROM extraction_plans WHERE template_signature = ?", (template_signature,)).fetchone()
            if not row:
                return None
            return ExtractionPlan(**json.loads(row["plan_json"]))

    def save_extraction_plan(self, plan: ExtractionPlan):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO extraction_plans (template_signature, plan_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(template_signature) DO UPDATE SET plan_json=excluded.plan_json
            """, (plan.template_signature, json.dumps(plan.model_dump()), now))
            conn.commit()

    # Events
    def log_event(self, job_id: int, message: str, level: str = "info", event_type: str = "general"):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
            INSERT INTO events (job_id, timestamp, level, message, event_type)
            VALUES (?, ?, ?, ?, ?)
            """, (job_id, now, level, message, event_type))
            conn.commit()

    def get_events(self, job_id: int, limit: int = 50) -> List[RunEvent]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE job_id = ? ORDER BY id DESC LIMIT ?",
                (job_id, limit)
            ).fetchall()
            return [
                RunEvent(
                    id=r["id"],
                    run_id=r["job_id"],
                    timestamp=r["timestamp"],
                    level=r["level"],
                    message=r["message"],
                    event_type=r["event_type"]
                )
                for r in reversed(rows)
            ]
