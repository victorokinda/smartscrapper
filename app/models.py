from enum import Enum
from typing import Optional, List, Dict, Any, Literal
from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    THROTTLED = "throttled"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class RecordStage(str, Enum):
    LISTING = "listing"
    DETAIL = "detail"


class DecisionOutcome(str, Enum):
    SELECT = "select"
    REJECT = "reject"
    NEEDS_DETAIL = "needs_detail"
    UNCERTAIN = "uncertain"


class AttachmentStatus(str, Enum):
    PENDING = "pending"
    DOWNLOADED = "downloaded"
    REUSED = "reused"
    FAILED = "failed"
    SKIPPED = "skipped"
    NO_ATTACHMENT = "no_attachment"


class Attachment(BaseModel):
    id: Optional[int] = None
    record_id: Optional[int] = None
    run_id: Optional[int] = None
    attachment_id: str
    original_name: str
    url: str
    local_path: Optional[str] = None
    sha256: Optional[str] = None
    byte_count: Optional[int] = None
    download_status: AttachmentStatus = AttachmentStatus.PENDING
    is_reused: bool = False
    error: Optional[str] = None


class Record(BaseModel):
    id: Optional[int] = None
    run_id: Optional[int] = None
    record_id: str
    source_url: str
    page_number: int = 0
    title: str = ""
    submitter: str = ""
    organization: Optional[str] = None
    proceeding: Optional[str] = None
    listing_description: Optional[str] = None
    detail_url: Optional[str] = None
    detail_description: Optional[str] = None
    stage: RecordStage = RecordStage.LISTING
    decision: Optional[DecisionOutcome] = None
    reason: Optional[str] = None
    decision_backend: Optional[str] = None
    decision_model: Optional[str] = None
    decision_probabilities: Optional[Dict[str, float]] = None
    status: str = "pending"  # pending, select, reject, needs_detail, uncertain, manual_included, manual_excluded
    evidence_used: Optional[Dict[str, Any]] = None
    attachments: List[Attachment] = Field(default_factory=list)


class DecisionRubric(BaseModel):
    eligibility_rules: List[str] = Field(default_factory=list)
    exclusions: List[str] = Field(default_factory=list)
    navigation_hints: List[str] = Field(default_factory=list)
    attachment_scope: str = "all_substantive"
    depth_limit: int = 1
    summary: str = ""
    raw_instructions: str = ""


class ExtractionPlan(BaseModel):
    template_signature: str
    container_type: Literal["table", "list", "cards"] = "table"
    row_selector: str
    title_selector: Optional[str] = None
    submitter_selector: Optional[str] = None
    org_selector: Optional[str] = None
    description_selector: Optional[str] = None
    detail_link_selector: Optional[str] = None
    attachment_link_selector: Optional[str] = None
    next_page_selector: Optional[str] = None
    is_verified_carb: bool = False


class RunStats(BaseModel):
    listing_pages_visited: int = 0
    detail_pages_visited: int = 0
    submissions_found: int = 0
    submissions_selected: int = 0
    submissions_rejected: int = 0
    submissions_uncertain: int = 0
    attachments_downloaded: int = 0
    attachments_failed: int = 0
    attachments_reused: int = 0
    unique_files: int = 0
    inference_calls: int = 0
    cache_hits: int = 0
    model_handoffs: int = 0
    total_runtime_seconds: float = 0.0


class RunEvent(BaseModel):
    id: Optional[int] = None
    run_id: int
    timestamp: str
    level: str = "info"  # info, warning, error, success
    message: str
    event_type: str = "general"


class Project(BaseModel):
    id: str
    name: str
    source_url: str
    instructions: str
    output_dir: str
    created_at: str
    updated_at: str


class Job(BaseModel):
    id: int
    project_id: str
    status: JobStatus = JobStatus.IDLE
    mode: Literal["decision", "qwen"] = "decision"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    stats: RunStats = Field(default_factory=RunStats)
    rubric: Optional[DecisionRubric] = None
    current_stage: str = "idle"
    limiter_throttled: bool = False
    cooldown_until: Optional[float] = None
