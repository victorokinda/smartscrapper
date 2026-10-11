import asyncio
import datetime
import time
from typing import Optional, Dict, Any, List
from app.config import settings
from app.database import Database
from app.downloader import Downloader
from app.limiter import RateLimiter, OriginBlockedError, OriginThrottledError
from app.models import (
    Job, JobStatus, Project, Record, RecordStage, DecisionOutcome,
    AttachmentStatus, RunStats
)
from app.network import NetworkClient
from app.ollama import OllamaClient
from app.parser import HtmlParser
from app.fetcher import SmartFetcher
from app.eu_feedback import EUFeedbackPortal


class Orchestrator:
    def __init__(
        self,
        db: Database,
        limiter: RateLimiter,
        net_client: NetworkClient,
        ollama_client: OllamaClient,
        downloader: Downloader
    ):
        self.db = db
        self.limiter = limiter
        self.net = net_client
        self.ollama = ollama_client
        self.downloader = downloader
        self.fetcher = SmartFetcher(self.net)
        self.active_jobs: Dict[int, asyncio.Task] = {}
        self.cancel_events: Dict[int, asyncio.Event] = {}

    def is_job_running(self, job_id: int) -> bool:
        task = self.active_jobs.get(job_id)
        return task is not None and not task.done()

    async def start_job(self, project: Project, job: Job) -> Job:
        if self.is_job_running(job.id):
            return job

        cancel_ev = asyncio.Event()
        self.cancel_events[job.id] = cancel_ev
        task = asyncio.create_task(self._run_job_pipeline(project, job, cancel_ev))
        self.active_jobs[job.id] = task
        return job

    async def cancel_job(self, job_id: int):
        cancel_ev = self.cancel_events.get(job_id)
        if cancel_ev:
            cancel_ev.set()
        task = self.active_jobs.get(job_id)
        if task and not task.done():
            task.cancel()
        job = self.db.get_job(job_id)
        if job and job.status == JobStatus.RUNNING:
            job.status = JobStatus.CANCELLED
            job.current_stage = "cancelled"
            job.finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.db.update_job(job)
            self.db.log_event(job.id, "Job was manually cancelled", level="warning", event_type="cancelled")

    async def resume_job(self, project: Project, job: Job) -> Job:
        job.status = JobStatus.RUNNING
        job.current_stage = "resuming"
        job.error = None
        self.db.update_job(job)
        self.db.log_event(job.id, "Resuming job execution", level="info", event_type="resume")
        return await self.start_job(project, job)

    async def _run_job_pipeline(self, project: Project, job: Job, cancel_event: asyncio.Event):
        start_time = time.time()
        job.status = JobStatus.RUNNING
        job.started_at = job.started_at or datetime.datetime.now(datetime.timezone.utc).isoformat()
        job.current_stage = "starting"
        self.db.update_job(job)
        self.db.log_event(job.id, f"Starting crawl job in {job.mode} mode", level="info", event_type="start")

        try:
            # 1. Planning phase: Compile rubric with Qwen if not already compiled
            if not job.rubric:
                job.current_stage = "planning_rubric"
                self.db.update_job(job)
                self.db.log_event(job.id, "Interpreting custom instructions with Qwen", level="info", event_type="planning")
                rubric = await self.ollama.compile_rubric(project.instructions)
                job.rubric = rubric
                self.db.update_job(job)
                self.db.log_event(job.id, f"Rubric compiled: {rubric.summary}", level="info", event_type="planning")

            # In decision mode: unload planner to prepare for Tev1 classification
            if job.mode == "decision" and settings.MODEL_RESIDENCY == "sequential":
                job.current_stage = "unloading_planner"
                self.db.update_job(job)
                self.db.log_event(job.id, "Unloading Qwen from GPU memory for sequential Tev1 residency", level="info", event_type="handoff")
                await self.ollama.switch_model_role("classifying", settings.CLASSIFIER_MODEL)

            # 2. Deterministic Crawling Loop
            job.current_stage = "crawling"
            self.db.update_job(job)

            current_url = project.source_url
            page_number = 0
            visited_pages = set()

            # Existing records if resuming
            existing_records = {r.record_id: r for r in self.db.get_records_for_job(job.id)}

            while current_url and page_number < settings.MAX_PAGES:
                if cancel_event.is_set():
                    break

                if current_url in visited_pages:
                    self.db.log_event(job.id, f"Loop detected: {current_url} already visited. Stopping traversal.", level="warning")
                    break
                visited_pages.add(current_url)

                is_eu = EUFeedbackPortal.is_eu_feedback_url(current_url)
                if is_eu:
                    self.db.log_event(job.id, f"Detected European Commission consultation portal: {current_url}", level="info", event_type="portal_detected")
                    try:
                        records, next_page_url, init_title = await EUFeedbackPortal.fetch_feedbacks(
                            current_url, self.net, page_number=page_number, cancel_event=cancel_event
                        )
                        if init_title and job.rubric and not job.rubric.summary:
                            job.rubric.summary = f"EU Consultation: {init_title}"
                        html = ""
                    except Exception as e:
                        self.db.log_event(job.id, f"EU feedback API extraction error: {e}. Falling back to general fetcher.", level="warning")
                        is_eu = False

                if not is_eu:
                    try:
                        html, meta = await self.fetcher.fetch_html(current_url, cancel_event=cancel_event)
                        if meta.get("escalated"):
                            self.db.log_event(job.id, "Dynamic SPA detected. Escalated page fetch to headless browser.", level="info", event_type="browser_escalation")
                    except OriginBlockedError as be:
                        job.status = JobStatus.FAILED
                        job.error = f"Blocked by origin: {be}"
                        self.db.log_event(job.id, job.error, level="error", event_type="blocked")
                        break
                    except Exception as fe:
                        self.db.log_event(job.id, f"Failed to fetch {current_url}: {fe}", level="error")
                        break

                    records, next_page_url = HtmlParser.parse_generic_listing(html, current_url, page_number)

                    # If 0 records were found on starting page, attempt browser escalation if not already tried
                    if not records and page_number == 0 and not meta.get("escalated") and settings.ENABLE_BROWSER_RENDERING:
                        self.db.log_event(job.id, "0 records found with static HTTP parser. Escalating to headless browser to inspect dynamic DOM...", level="info", event_type="browser_escalation")
                        try:
                            browser_html, b_meta = await self.fetcher.fetch_html(current_url, cancel_event=cancel_event, force_browser=True)
                            if b_meta.get("escalated"):
                                html = browser_html
                                records, next_page_url = HtmlParser.parse_generic_listing(html, current_url, page_number)
                        except Exception as be:
                            self.db.log_event(job.id, f"Browser escalation attempt failed: {be}", level="warning")

                    # If still 0 records on starting page, log rich DOM diagnostics
                    if not records and page_number == 0:
                        diagnosis = HtmlParser.diagnose_empty_listing(html, current_url)
                        self.db.log_event(job.id, diagnosis, level="warning", event_type="diagnosis")

                job.stats.listing_pages_visited += 1

                for record in records:
                    if cancel_event.is_set():
                        break

                    record.run_id = job.id
                    # If resuming and already decided, skip inference
                    if record.record_id in existing_records:
                        old_r = existing_records[record.record_id]
                        if old_r.decision is not None:
                            record = old_r

                    job.stats.submissions_found += 1

                    # 3. Decision Pipeline
                    if record.decision is None:
                        # Stage 1: Listing decision
                        listing_evidence = {
                            "title": record.title,
                            "submitter": record.submitter,
                            "organization": record.organization,
                            "description": record.listing_description,
                            "attachments": [a.original_name for a in record.attachments],
                            "detail_url": record.detail_url
                        }
                        outcome, reason, probs, is_cache = await self.ollama.classify(
                            record_data=listing_evidence,
                            rubric=job.rubric,
                            stage=RecordStage.LISTING,
                            backend=job.mode
                        )
                        if is_cache:
                            job.stats.cache_hits += 1
                        else:
                            job.stats.inference_calls += 1

                        record.decision = outcome
                        record.reason = reason
                        record.decision_backend = job.mode
                        record.decision_model = settings.CLASSIFIER_MODEL if job.mode == "decision" else settings.QWEN_ONLY_MODEL
                        record.decision_probabilities = probs
                        record.status = outcome.value
                        record.evidence_used = listing_evidence

                        # If NEEDS_DETAIL: follow detail link
                        if outcome == DecisionOutcome.NEEDS_DETAIL and record.detail_url:
                            self.db.log_event(job.id, f"Record {record.record_id} needs detail; visiting {record.detail_url}", level="info")
                            try:
                                d_resp = await self.net.fetch(record.detail_url, cancel_event)
                                job.stats.detail_pages_visited += 1
                                d_desc, d_atts = HtmlParser.parse_generic_detail(d_resp.text, record.detail_url)
                                record.detail_description = d_desc
                                if d_atts:
                                    record.attachments.extend(d_atts)

                                # Stage 2: Detail decision
                                detail_evidence = {
                                    "title": record.title,
                                    "submitter": record.submitter,
                                    "organization": record.organization,
                                    "description": f"{record.listing_description or ''}\n\nDetail: {d_desc}",
                                    "attachments": [a.original_name for a in record.attachments],
                                    "detail_url": record.detail_url
                                }
                                d_outcome, d_reason, d_probs, d_cache = await self.ollama.classify(
                                    record_data=detail_evidence,
                                    rubric=job.rubric,
                                    stage=RecordStage.DETAIL,
                                    backend=job.mode
                                )
                                if d_cache:
                                    job.stats.cache_hits += 1
                                else:
                                    job.stats.inference_calls += 1

                                record.stage = RecordStage.DETAIL
                                record.decision = d_outcome
                                record.reason = d_reason
                                record.decision_probabilities = d_probs
                                record.status = d_outcome.value
                                record.evidence_used = detail_evidence
                            except Exception as de:
                                self.db.log_event(job.id, f"Failed to fetch detail for {record.record_id}: {de}", level="warning")
                                record.decision = DecisionOutcome.UNCERTAIN
                                record.status = "uncertain"

                        # If SELECT but no direct attachments, check if detail page has attachments
                        elif outcome == DecisionOutcome.SELECT and not record.attachments and record.detail_url:
                            try:
                                d_resp = await self.net.fetch(record.detail_url, cancel_event)
                                job.stats.detail_pages_visited += 1
                                _, d_atts = HtmlParser.parse_generic_detail(d_resp.text, record.detail_url)
                                if d_atts:
                                    record.attachments.extend(d_atts)
                            except Exception as de:
                                self.db.log_event(job.id, f"Failed to discover attachments on detail page for {record.record_id}: {de}", level="warning")

                    # Update counts
                    if record.decision == DecisionOutcome.SELECT:
                        job.stats.submissions_selected += 1
                    elif record.decision == DecisionOutcome.REJECT:
                        job.stats.submissions_rejected += 1
                    else:
                        job.stats.submissions_uncertain += 1

                    # Save record to database
                    record = self.db.save_record(record)
                    existing_records[record.record_id] = record

                    # 4. Immediate downloading for selected attachments
                    if record.decision == DecisionOutcome.SELECT and record.attachments:
                        for att in record.attachments:
                            if cancel_event.is_set():
                                break
                            if att.download_status in (AttachmentStatus.DOWNLOADED, AttachmentStatus.REUSED):
                                continue
                            try:
                                await self.downloader.download_attachment(att, project, job.id, cancel_event)
                                if att.is_reused:
                                    job.stats.attachments_reused += 1
                                else:
                                    job.stats.attachments_downloaded += 1
                                    job.stats.unique_files += 1
                                self.db.log_event(job.id, f"Downloaded {att.original_name}", level="success", event_type="download")
                            except Exception as de:
                                job.stats.attachments_failed += 1
                                self.db.log_event(job.id, f"Failed to download {att.original_name}: {de}", level="warning", event_type="download")

                    # Checkpoint stats after each record
                    job.stats.total_runtime_seconds = round(time.time() - start_time, 1)
                    self.db.update_job(job)

                # Move to next page
                if next_page_url and not cancel_event.is_set():
                    current_url = next_page_url
                    page_number += 1
                else:
                    self.db.log_event(job.id, f"Traversal complete at page {page_number}. No further pages.", level="info")
                    break

            # 5. Pipeline completion
            all_records = self.db.get_records_for_job(job.id)
            self.downloader.export_manifests(project, job, all_records)

            if cancel_event.is_set():
                job.status = JobStatus.CANCELLED
                job.current_stage = "cancelled"
                self.db.log_event(job.id, "Job was cancelled", level="warning", event_type="cancelled")
            else:
                job.status = JobStatus.COMPLETED
                job.current_stage = "completed"
                self.db.log_event(job.id, "Job completed successfully", level="success", event_type="complete")

        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.current_stage = "cancelled"
            self.db.log_event(job.id, "Job cancelled by user", level="warning")
        except Exception as e:
            job.status = JobStatus.FAILED
            job.current_stage = "failed"
            job.error = str(e)
            self.db.log_event(job.id, f"Job failed with error: {e}", level="error")
        finally:
            job.finished_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            job.stats.total_runtime_seconds = round(time.time() - start_time, 1)
            self.db.update_job(job)
            if job.id in self.active_jobs:
                del self.active_jobs[job.id]
            if job.id in self.cancel_events:
                del self.cancel_events[job.id]
