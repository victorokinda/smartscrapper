import asyncio
import hashlib
import json
import time
from typing import Dict, Any, Optional, Tuple, List
import httpx
from app.config import settings
from app.database import Database
from app.models import DecisionRubric, DecisionOutcome, RecordStage, ExtractionPlan


class OllamaClient:
    def __init__(self, db: Database, base_url: Optional[str] = None):
        self.db = db
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self._lock = asyncio.Lock()
        self.current_role: str = "idle"  # idle, planning, unloading_planner, classifying, repairing_template
        self.currently_resident_model: Optional[str] = None

    async def get_loaded_models(self) -> List[str]:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(f"{self.base_url}/api/ps")
                if res.status_code == 200:
                    data = res.json()
                    return [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            pass
        return []

    async def is_model_loaded(self, model_name: str) -> bool:
        loaded = await self.get_loaded_models()
        return any(model_name in m for m in loaded)

    async def unload_model(self, model_name: str, timeout: Optional[float] = None) -> bool:
        to = timeout or settings.MODEL_HANDOFF_TIMEOUT_SECONDS
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Documented empty generate request with keep_alive=0
                await client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": model_name, "keep_alive": 0}
                )
        except Exception:
            pass

        # Poll /api/ps until model is gone
        start = time.time()
        while time.time() - start < to:
            if not await self.is_model_loaded(model_name):
                if self.currently_resident_model == model_name:
                    self.currently_resident_model = None
                return True
            await asyncio.sleep(0.5)
        return False

    async def switch_model_role(self, target_role: str, target_model: str):
        if settings.MODEL_RESIDENCY != "sequential":
            self.current_role = target_role
            self.currently_resident_model = target_model
            return

        if self.currently_resident_model and self.currently_resident_model != target_model:
            # Need to unload current resident model first
            prev_role = self.current_role
            self.current_role = f"unloading_{self.currently_resident_model}"
            unloaded = await self.unload_model(self.currently_resident_model)
            if not unloaded:
                self.current_role = prev_role
                raise RuntimeError(
                    f"Failed to unload {self.currently_resident_model} within {settings.MODEL_HANDOFF_TIMEOUT_SECONDS}s"
                )
        self.current_role = target_role
        self.currently_resident_model = target_model

    # --- Planning with Qwen ---
    async def compile_rubric(self, instructions: str) -> DecisionRubric:
        async with self._lock:
            await self.switch_model_role("planning", settings.PLANNER_MODEL)
            prompt = f"""You are an expert scraping and classification planner.
Given the following user project instructions, compile a structured decision rubric.
Separate eligibility rules, exclusions, website navigation hints, and attachment scope.
Preserve all qualifiers, conditions, and AND/OR criteria accurately. Do not invent rules.

User Instructions:
\"\"\"{instructions}\"\"\"

Output valid JSON matching this exact structure:
{{
  "eligibility_rules": ["string", ...],
  "exclusions": ["string", ...],
  "navigation_hints": ["string", ...],
  "attachment_scope": "all_substantive" or specific scope,
  "depth_limit": 1,
  "summary": "Brief 1-2 sentence summary of the criteria"
}}"""
            payload = {
                "model": settings.PLANNER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": settings.PLANNER_THINKING,
                "format": "json",
                "options": {
                    "temperature": 0,
                    "num_ctx": settings.PLANNER_CONTEXT_TOKENS,
                    "num_predict": settings.PLANNER_MAX_OUTPUT_TOKENS
                }
            }
            async with httpx.AsyncClient(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
                res = await client.post(f"{self.base_url}/api/chat", json=payload)
                res.raise_for_status()
                data = res.json()
                content = data.get("message", {}).get("content", "{}")
                try:
                    parsed = json.loads(content)
                except Exception:
                    # Clean markdown code blocks if any
                    clean = content.replace("```json", "").replace("```", "").strip()
                    parsed = json.loads(clean)

                rubric = DecisionRubric(
                    eligibility_rules=parsed.get("eligibility_rules", []),
                    exclusions=parsed.get("exclusions", []),
                    navigation_hints=parsed.get("navigation_hints", []),
                    attachment_scope=parsed.get("attachment_scope", "all_substantive"),
                    depth_limit=int(parsed.get("depth_limit", 1)),
                    summary=parsed.get("summary", ""),
                    raw_instructions=instructions
                )
                return rubric

    # --- Template discovery with Qwen ---
    async def discover_extraction_plan(self, dom_skeleton: Dict[str, Any], hints: List[str]) -> ExtractionPlan:
        async with self._lock:
            await self.switch_model_role("repairing_template", settings.PLANNER_MODEL)
            prompt = f"""You are an expert web scraping parser planner.
Given observed candidates in a DOM skeleton and user navigation hints, select the best candidate extraction plan.
You must choose ONLY from observed candidate IDs. Do not invent IDs or selectors.

DOM Candidates:
{json.dumps(dom_skeleton, indent=2)}

Navigation Hints:
{json.dumps(hints, indent=2)}

Return valid JSON matching:
{{
  "container_type": "table" or "cards",
  "chosen_candidate_id": "table_1",
  "title_field": "observed header or label",
  "submitter_field": "observed header or label",
  "attachment_field": "observed header or label",
  "detail_link_field": "observed header or label"
}}"""
            payload = {
                "model": settings.PLANNER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": settings.PLANNER_THINKING,
                "format": "json",
                "options": {
                    "temperature": 0,
                    "num_ctx": settings.PLANNER_CONTEXT_TOKENS,
                    "num_predict": settings.PLANNER_MAX_OUTPUT_TOKENS
                }
            }
            async with httpx.AsyncClient(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
                res = await client.post(f"{self.base_url}/api/chat", json=payload)
                res.raise_for_status()
                parsed = json.loads(res.json().get("message", {}).get("content", "{}"))
                return ExtractionPlan(
                    template_signature="discovered",
                    container_type=parsed.get("container_type", "table"),
                    row_selector="tr",
                    title_selector=parsed.get("title_field"),
                    submitter_selector=parsed.get("submitter_field"),
                    attachment_link_selector=parsed.get("attachment_field"),
                    detail_link_selector=parsed.get("detail_link_field")
                )

    # --- Classification ---
    async def classify(
        self,
        record_data: Dict[str, Any],
        rubric: DecisionRubric,
        stage: RecordStage,
        backend: str = "decision"
    ) -> Tuple[DecisionOutcome, str, Optional[Dict[str, float]], bool]:
        """
        Returns (outcome, reason, probabilities, is_cache_hit)
        """
        # Build cache key
        rubric_hash = hashlib.sha256(json.dumps(rubric.model_dump()).encode()).hexdigest()[:16]
        evidence_str = f"{record_data.get('title','')}|{record_data.get('submitter','')}|{record_data.get('description','')}|{record_data.get('attachments','')}"
        evidence_hash = hashlib.sha256(evidence_str.encode()).hexdigest()[:16]
        cache_key = f"{stage.value}_{rubric_hash}_{evidence_hash}_{backend}"

        cached = self.db.get_cached_decision(cache_key)
        if cached:
            return DecisionOutcome(cached["decision"]), cached["reason"] or "", cached.get("probabilities"), True

        async with self._lock:
            # Recheck cache inside lock
            cached = self.db.get_cached_decision(cache_key)
            if cached:
                return DecisionOutcome(cached["decision"]), cached["reason"] or "", cached.get("probabilities"), True

            if backend == "decision":
                outcome, reason, probs = await self._classify_tev1(record_data, rubric, stage)
            else:
                outcome, reason, probs = await self._classify_qwen(record_data, rubric, stage)

            # Persist to cache
            self.db.set_cached_decision(
                cache_key=cache_key,
                stage=stage.value,
                rubric_hash=rubric_hash,
                evidence_hash=evidence_hash,
                backend=backend,
                model=settings.CLASSIFIER_MODEL if backend == "decision" else settings.QWEN_ONLY_MODEL,
                decision=outcome.value,
                reason=reason,
                probabilities=probs
            )
            return outcome, reason, probs, False

    async def _classify_tev1(
        self,
        record_data: Dict[str, Any],
        rubric: DecisionRubric,
        stage: RecordStage
    ) -> Tuple[DecisionOutcome, str, Optional[Dict[str, float]]]:
        await self.switch_model_role("classifying", settings.CLASSIFIER_MODEL)

        description = record_data.get("description", "") or ""
        # Windowing if description exceeds client budget (~5000 chars)
        max_chars = settings.CLASSIFIER_INPUT_BUDGET_TOKENS * 3
        if len(description) > max_chars:
            return await self._classify_tev1_windowed(record_data, rubric, stage, max_chars)

        state = {
            "title": record_data.get("title", ""),
            "submitter": record_data.get("submitter", ""),
            "organization": record_data.get("organization", ""),
            "description": description,
            "attachments": record_data.get("attachments", []),
            "stage": stage.value,
            "has_detail_link": bool(record_data.get("detail_url"))
        }

        criteria = {
            "select": "Visible evidence reasonably supports all requested inclusion conditions without an explicit exclusion.",
            "reject": "Visible evidence shows a clear nonmatch or explicit exclusion.",
            "uncertain": "Evidence is insufficient and unresolved from visible information."
        }
        if stage == RecordStage.LISTING:
            criteria["needs_detail"] = "Evidence is insufficient on listing, a detail page link exists, and detail description may resolve eligibility."

        instructions = f"Eligibility rubric: {rubric.summary}. Rules: {'; '.join(rubric.eligibility_rules)}. Exclude: {'; '.join(rubric.exclusions)}."
        payload = {
            "model": settings.CLASSIFIER_MODEL,
            "state": state,
            "questions": {
                "decision": {
                    "instructions": instructions,
                    "type": "choice",
                    "criteria": criteria
                }
            },
            "keep_alive": settings.CLASSIFIER_KEEP_ALIVE
        }

        async with httpx.AsyncClient(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
            res = await client.post(f"{self.base_url}/v1/systemone", json=payload)
            res.raise_for_status()
            data = res.json()
            # Response structure: {"model": "tev1:4b", "answers": {"decision": {"choice": "...", "probabilities": {...}}}}
            answers_dict = data.get("answers") or data.get("questions") or {}
            q_res = answers_dict.get("decision", {})
            choice = q_res.get("choice", "uncertain")
            probs = q_res.get("probabilities", {})

            # Map choice to DecisionOutcome
            if choice == "select":
                outcome = DecisionOutcome.SELECT
                reason = "Selected from visible evidence"
            elif choice == "reject":
                outcome = DecisionOutcome.REJECT
                reason = "Rejected: nonmatch or explicit exclusion"
            elif choice == "needs_detail" and stage == RecordStage.LISTING:
                outcome = DecisionOutcome.NEEDS_DETAIL
                reason = "Needs submission detail page to evaluate"
            else:
                outcome = DecisionOutcome.UNCERTAIN
                reason = "Skipped: insufficient visible evidence"

            return outcome, reason, probs

    async def _classify_tev1_windowed(
        self,
        record_data: Dict[str, Any],
        rubric: DecisionRubric,
        stage: RecordStage,
        window_size: int
    ) -> Tuple[DecisionOutcome, str, Optional[Dict[str, float]]]:
        full_text = record_data.get("description", "")
        stride = int(window_size * 0.75)
        windows = []
        for i in range(0, len(full_text), stride):
            windows.append(full_text[i:i + window_size])
            if len(windows) >= settings.MAX_DESCRIPTION_WINDOWS:
                break

        outcomes = []
        for w in windows:
            w_rec = dict(record_data)
            w_rec["description"] = w
            out, r, p = await self._classify_tev1(w_rec, rubric, stage)
            outcomes.append(out)

        # Evaluate window consensus
        if all(o == DecisionOutcome.REJECT for o in outcomes):
            return DecisionOutcome.REJECT, "Rejected: all inspected description windows rejected", None
        if any(o == DecisionOutcome.SELECT for o in outcomes) and not any(o == DecisionOutcome.REJECT for o in outcomes):
            return DecisionOutcome.SELECT, "Selected: positive evidence found in window without exclusions", None
        return DecisionOutcome.UNCERTAIN, "Uncertain: window evidence mixed or reached limit", None

    async def _classify_qwen(
        self,
        record_data: Dict[str, Any],
        rubric: DecisionRubric,
        stage: RecordStage
    ) -> Tuple[DecisionOutcome, str, Optional[Dict[str, float]]]:
        await self.switch_model_role("classifying", settings.QWEN_ONLY_MODEL)
        allowed_choices = ["select", "reject", "uncertain"]
        if stage == RecordStage.LISTING:
            allowed_choices.append("needs_detail")

        prompt = f"""You are a precise document selection classifier.
Eligibility Criteria:
{rubric.summary}
Rules: {json.dumps(rubric.eligibility_rules)}
Exclusions: {json.dumps(rubric.exclusions)}

Submission Data:
Title: {record_data.get('title','')}
Submitter: {record_data.get('submitter','')}
Organization: {record_data.get('organization','')}
Description: {record_data.get('description','')}
Attachments: {json.dumps(record_data.get('attachments',[]))}
Stage: {stage.value}
Has Detail Link: {bool(record_data.get('detail_url'))}

Evaluate the evidence against the criteria.
Allowed decisions: {json.dumps(allowed_choices)}.
Output valid JSON:
{{
  "decision": "{'|'.join(allowed_choices)}",
  "reason": "Brief evidence-based explanation (1 sentence)"
}}"""

        payload = {
            "model": settings.QWEN_ONLY_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "think": settings.PLANNER_THINKING,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_ctx": settings.PLANNER_CONTEXT_TOKENS,
                "num_predict": 256
            }
        }

        async with httpx.AsyncClient(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
            res = await client.post(f"{self.base_url}/api/chat", json=payload)
            res.raise_for_status()
            data = res.json()
            content = data.get("message", {}).get("content", "{}")
            try:
                parsed = json.loads(content)
            except Exception:
                clean = content.replace("```json", "").replace("```", "").strip()
                parsed = json.loads(clean)

            d = parsed.get("decision", "uncertain").lower()
            reason = parsed.get("reason", "Qwen evaluation")
            if d == "select":
                return DecisionOutcome.SELECT, reason, None
            elif d == "reject":
                return DecisionOutcome.REJECT, reason, None
            elif d == "needs_detail" and stage == RecordStage.LISTING:
                return DecisionOutcome.NEEDS_DETAIL, reason, None
            else:
                return DecisionOutcome.UNCERTAIN, reason, None
