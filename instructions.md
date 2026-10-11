# Build instructions: Local smart document scraper

You are my local coding agent. Build and test a working, lightweight, single-page web app that discovers public document submissions, uses local Qwen for setup and Tev1 for repeated selection decisions, and downloads the matching attachments. Deliver the implementation, not merely a plan.

## 1. Goal and boundaries

I enter a project name, starting URL and custom instructions, start a job, and monitor its progress on one page. Instructions can contain both selection criteria and website-specific navigation hints. Python handles fetching, parsing, pagination, persistence and downloading. Qwen interprets instructions and assists with bounded extraction-plan discovery. Tev1 makes repeated relevance decisions from visible listing/detail-page information. Selection must happen before downloading attachment bodies. Python is the orchestrator; neither model controls the crawl loop.

Optimise for useful best-effort coverage rather than exhaustive collection. I want most likely matches, not every possible file or a research-grade identity verification exercise. Select when the visible text reasonably supports the criteria, reject clear nonmatches, and skip genuinely unresolved cases with a short reason. Keep an optional review queue, but never require me to resolve it to finish. Do not search other websites to verify identities. Selection is a best-effort inference from source text, not a guarantee of complete coverage.

My workstation has an NVIDIA RTX 3080 Laptop GPU, reported 16 GB VRAM, Intel i7-11850H and 80 GB system RAM. Verify available GPU memory locally with `nvidia-smi`; the Windows NFO memory field is malformed and must not be treated as a VRAM measurement. My existing Ollama container is `agent_ollama`, and the expected installed planner is `qwen3:14b` at Q4_K_M. Inspect the actual model tag/digest/quantisation and container network before configuring the app.

Reuse this Ollama service. The requested new approach authorises pulling `tev1:4b` into it if missing; do not ask again merely to download that model. Do not pull Nimble, a smaller Qwen or other additional models by default. Tev1's documented Ollama integration requires version 0.35 or later. Verify the installed version and `/v1/systemone` availability. If an upgrade is needed, identify a compatible version, preserve volumes, GPU access, networking and existing configuration, and document the exact minimal upgrade. Do not replace a running shared service or interrupt unrelated workloads silently. Complete the Qwen-only path and remaining implementation if a service upgrade is blocked; report the dual-model path as unverified rather than pretending it works. The application must never auto-pull models or upgrade Ollama on normal startup.

The default is sequential residency: Qwen14B for setup, explicit unload, Tev1 for classification. Do not depend on both models fitting simultaneously. Keep planner, classifier and backend configurable.

Prioritise efficient inference on a small, slow local model. Do not build a general autonomous browser agent, introduce LangChain, Redis, Celery, a vector database, a frontend build pipeline, or a separate worker container. One Python application container, one persistent SQLite database, and my existing Ollama service are sufficient. Use FastAPI, httpx, BeautifulSoup or lxml, Pydantic, and plain HTML/CSS/JavaScript, or similarly lightweight equivalents.

## 2. First use case: CARB comments

Starting URL:
`https://ww2.arb.ca.gov/approved-comments?entity_id=41096&page=0`

The reported columns are Name, Subject, Submitted to, File upload, and Comment. Verify the actual live DOM locally before choosing selectors. Inspect whether Comment links lead to useful detail pages, whether a row has several attachments, and how pagination terminates. Do not assume these details have already been verified.

Preload an editable example selection instruction:

> Download comment attachments concerning California SB 253 submitted by industry associations; institutional investors and financial coalitions; environmental NGOs; environmental justice and community coalitions; accounting, auditing and professional standards bodies; labor unions and labor coalitions; academic centers; think tanks; and policy clinics. Include joint SB 253/SB 261 submissions when SB 253 is explicitly covered. Exclude submissions concerning SB 261 alone, unrelated proceedings, individuals speaking solely in a personal capacity, and individual commercial companies unless they fall within one of the requested categories. Use subject, submitter information and attachment filenames as evidence. Put cases with insufficient evidence into Review rather than guessing.

Treat topic relevance and submitter eligibility as separate conditions when the instructions require both. Visible descriptions, affiliations, signatures, subject lines and filenames may support a reasonable inference; do not demand independent verification. A personal name alone does not establish personal capacity or institutional affiliation. Do not invent organisations or treat the model's background recollection as source evidence. In other projects, do not require submitter eligibility unless the instructions specify it.

Classify at submission/row level. Download all substantive comment attachments associated with a selected submission by default, retaining their individual filenames and URLs. Do not select logos, navigation links, website assets, or unrelated linked references. Preserve rows without attachments in the manifest as `no_attachment`; do not claim to have downloaded their comment files.

## 3. Architecture and tool boundary

Implement a small deterministic orchestrator with narrow Python functions such as:

- `fetch_page(url)` → cached response and fetch metadata.
- `discover_extraction_plan(page, navigation_hints)` → validated reusable listing/detail plan.
- `extract_records(page, plan_or_adapter)` → normalised records and pagination links.
- `fetch_record_details(record_id)` → required attachment discovery and optional relevance enrichment.
- `classify_record(record, criteria, stage)` → validated Tev1 decision, or Qwen decision in the configured fallback mode.
- `switch_model_role(role)` → serialised model handoff with residency checks.
- `download_attachment(attachment_id)` → verified local file and metadata.

Expose these as reusable internal tool functions with explicit schemas so the scraper and classifier are easy to extend. In the default pipeline, the orchestrator calls Python functions directly and passes compact records to the configured classifier. It interprets validated selection decisions as requests to download known attachment IDs. Do not spend inference tokens making the model repeatedly call fetch/next-page/download tools for actions Python can determine itself. Native model tool-calling support must not be a requirement.

The model must never invent URLs, shell commands, download paths or arbitrary executable parser code. During discovery it may choose only from observed DOM candidate IDs or parser-generated selector candidates. Resolve those choices, record IDs and attachment IDs to actual DOM elements and URLs entirely in Python. Keep source content as untrusted data, separate from the user's instructions. Validate IDs, enums, schema, expected record count, and duplicate/missing decisions before taking action.

## 4. Deterministic crawling and parsing

Build a reusable extraction engine supporting tables, repeated list items/cards, pagination, direct attachment links and listing-to-submission-detail-to-attachment navigation. Include a verified CARB preset, but make automatic discovery with natural-language navigation hints the normal route for unfamiliar websites. A new ordinary table/list source should not require code edits.

Python first identifies candidate tables, repeated record containers, headings, descriptions, anchor labels and pagination controls. If deterministic detection is ambiguous, give Qwen a compact annotated DOM skeleton containing observed node IDs, hierarchy, relevant labels and sample records. Remove scripts, styles, navigation/footer noise and redundant attributes, while preserving necessary structural relationships. Ask it once per distinct page template to choose an extraction plan from observed candidates: record container, title, description, submitter, detail link, attachment region, and next-page control. Validate the plan in Python against the page and a subsequent page where available, then reuse it deterministically. Cache by project, instructions/navigation hints and template signature. Permit at most one bounded repair attempt per failed template, then report a partial/unsupported extraction. Do not invoke the model to reinvent scraping instructions on every page.

Separate listing and detail-page templates. Resolve relative URLs correctly. File links may use opaque endpoint paths without extensions: use anchor text, download attributes and placement in the submission's document region as evidence; do not select only URLs ending in `.pdf`. Keep observed download endpoints and query parameters intact.

Implement this two-stage decision pipeline:

1. Extract compact title, description, submitter and observed links from each listing item.
2. Classify the listing evidence as `select`, `reject` or `needs_detail`. Reject clear nonmatches without visiting their detail pages. Treat uncertainty caused by short/missing listing descriptions as `needs_detail` where a detail link exists.
3. For `select` items with direct attachment links, download the selected attachments. For `select` items whose attachments are on a detail page, visit that page to discover the files even if no additional relevance inference is necessary.
4. For `needs_detail` items, fetch the submission page, extract the longer main description, affiliations and actual attachment links, and make a final selection decision from that visible information.
5. Download only after the relevant decision is `select`. If evidence remains insufficient, record `review/skipped_uncertain` and continue. If a selected detail page contains individually distinct documents, use labels/descriptions to apply explicit file-level instructions; otherwise download its substantive submission attachments.

Listing-to-detail navigation is core functionality, not optional review enrichment. A missing attachment on a listing is not `no_attachment` until the required detail-page discovery has run. Follow at most one submission-detail hop by default; make a second explicit document-landing hop configurable with a strict depth cap, not an unrestricted recursive crawl. Do not interpret ordinary external references inside descriptions as submission-detail links.

Preserve `entity_id=41096` and other relevant filters while traversing CARB pagination. Follow observed next-page links deterministically; if page-number generation is necessary, verify that pattern from the live site first. Track canonical page URLs and row signatures to detect loops/repeated pages. Do not drop query parameters that identify different submissions. Distinguish a genuinely empty final page from a failed request, blocked page, or broken parser. Record why traversal stopped. An empty extraction on a nonempty listing is an error, not successful completion.

Normalise each record with stable ID, listing URL, page number, submitter name, organisation if explicitly provided, title/subject, proceeding, listing description, detail URL, extracted main detail description, and attachment IDs/labels/filenames/URLs. Keep source metadata locally; the LLM receives only the fields needed for the current decision stage. Deduplicate records across pages using source IDs or stable detail URLs where available.

Implement a shared per-origin rate limiter covering listing requests, detail requests, file downloads, redirects and retries. Independent async workers must not bypass it. Default to a minimum two-second interval between request starts, add 0–1 seconds of jitter, permit no initial burst and allow one in-flight request per origin. Honour stricter robots directives where applicable. An origin means scheme, hostname and port; different paths share a limiter. Keep an additional global concurrency cap of two, and apply the same limits to attachment/CDN origins.

Use bounded retries, timeouts, a descriptive User-Agent and robots.txt checks. Honour Retry-After in both delta-seconds and HTTP-date forms; suspend requests to the affected origin until its cooldown ends. On 429/503 without Retry-After, exponentially back off with jitter, starting around 15 seconds and capped at five minutes. Allow at most three retries. Repeated throttling triggers a visible paused/throttled status requiring Resume rather than a retry storm. Treat 403/CAPTCHA/block responses as blocked; stop the affected origin rather than retrying or rotating identities. Persist cooldown deadlines across restart. Show rate, next retry and throttled state in the UI. Use asynchronous cancellable waits so the UI stays responsive. Do not bypass login or access controls. Rate limiting reduces pressure but cannot guarantee that a site will not block requests.

The EU CSRD scenario is a paginated submission list: each item has a short description and a submission link; its detail page contains a longer description and the actual file link near the bottom. Support and test that pattern as a core flow. No exact EU URL has been supplied, so validate this flow with representative fixtures and do not claim live compatibility with a specific portal.

The instructions box must accept navigation hints such as: 'This is a paginated list. Use the description to shortlist submissions. Open each potentially relevant submission's title link. Read its longer description and download the attached response document near the bottom. Ignore links to general background documents. Only include CSRD responses from the specified stakeholder types.' Parse hints separately from eligibility rules without making users write CSS or XPath.

Do not hard-code SB 253, CSRD or organisation categories into the generic engine. Ordinary table/list sources should work through discovery; site adapters are a fallback. Detect JavaScript-only pages and report the limitation clearly. Provide an optional Playwright rendering extra/profile only if needed, disabled by default; use Python/browser automation deterministically, not a per-click LLM agent. Reuse a public JSON endpoint only when actually observed and validate its pagination. Any browser fallback must retain throttling, redirect/URL controls and blocking detection; do not allow unbounded background API requests to bypass limits. Authentication/CAPTCHAs remain unsupported. Do not silently report success with zero records when parsing failed.

## 5. Two-tier inference and GPU residency

### 5.1 Responsibilities and operating modes

Implement two classifier backends behind one internal interface:

- `decision`: Qwen14B plans; Tev1 4B classifies through Ollama `/v1/systemone`. This is the requested target mode.
- `qwen`: Qwen14B handles planning and compact schema-constrained classification through `/api/chat`. This is the explicit baseline/fallback mode and must work without Tev1.

The UI and manifests must show the active mode and actual model identities. If decision mode is unavailable, offer a simple switch to Qwen-only; do not silently substitute models. Do not require hosted Jev, cloud credentials, another inference server, or another Ollama container.

Qwen is for instruction interpretation, first-time listing/detail-template discovery and bounded repair. Tev1 chooses among supplied labels; it does not generate parser plans, explanations, nested JSON, quotes, URLs or tool calls. Python handles all HTTP operations, navigation, state transitions, file paths, validation and downloads.

### 5.2 Planning phase

Use one Qwen instruction-interpretation call per new instruction set. Separate eligibility rules, exclusions, website navigation hints, attachment scope and optional depth/page limits. Compile a compact reusable decision rubric, accurately preserving qualifiers, exceptions and AND/OR conditions. Cache it; show its summary in the UI without an approval gate. Preserve the original instructions in the project/run snapshot as authoritative. No irreversible keyword exclusion rules inferred by Qwen.

Use `/api/chat`, `stream=false`, a JSON schema and Pydantic validation. Default planner context: 4096 tokens; output cap: 1024; temperature: 0; thinking disabled through the installed API's supported mechanism. Validate capability/version before sending parameters. Do not expose chain-of-thought.

Qwen receives only a compact annotated DOM skeleton when structural discovery is needed. Python supplies observed candidate IDs, hierarchy, useful labels and a few examples. Retain enough structure to locate repeated items, detail links, pagination and attachment regions; exclude scripts, CSS, boilerplate and redundant attributes. Validate returned candidate IDs and the resulting extraction plan before using it. Cache by template signature and relevant instructions. Do not assume detail pages share the listing template.

### 5.3 Serial model handoff

Implement a model manager with an async lock and explicit states such as `planning`, `unloading_planner`, `classifying`, and `repairing_template`. Permit only one model inference request at a time, including background work and manual actions.

After the planning phase, explicitly unload the planner using the documented empty Ollama generate/chat request with `keep_alive=0`. Confirm its disappearance from `/api/ps` within a bounded timeout before issuing Tev1 requests. Use documented unload semantics for the installed API. Before reloading Qwen for a new/failed template, similarly unload the classifier and verify it is gone. Never use `docker restart`, kill inference processes, or unload unrelated models as a handoff mechanism.

Cache plans and rubric so Qwen does not reload on every page or submission. Discover detail templates lazily when a selected/needs-detail item first requires one. Group pending unknown/failed templates into a bounded repair phase instead of switching per record. Use a small bounded work queue, prioritise its earliest blocked items, and then return to classification. Typical steady state should keep only Tev1 resident. HTTP crawling/downloads can continue during inference if they do not require the missing plan and the queues remain bounded.

Before modifying residency, detect obvious unrelated active work where possible; do not claim an application lock protects other Ollama clients. Document that exclusive scheduling is guaranteed only for this app. On memory pressure, queue conflicts or handoff failure, show a recoverable status; do not force CPU offload or continually retry loading both models. Persist pending work before handoff so interruption is resumable.

Use a bounded classifier keep-alive, for example 10 minutes, then allow idle unloading. This app's `.env` configures its requests; Ollama server variables such as `OLLAMA_NUM_PARALLEL` and `OLLAMA_MAX_LOADED_MODELS` must be configured on `agent_ollama` if needed, not added ineffectively to the scraper container. Recommend parallelism one for this workload, but inspect and document existing server settings rather than silently changing a shared service. `OLLAMA_MAX_LOADED_MODELS=1` is optional if that service is dedicated to this app.

### 5.4 Tev1 decision contract

Call `/v1/systemone` with `model=tev1:4b`, a compact `state`, named `questions` and configurable `keep_alive`. Prefer direct httpx requests to avoid SDK-version mismatch; use the TypeSafe SDK only if its installed version is compatible. Do not assume the Ollama chat API's JSON-schema, `num_ctx`, `think` or other options are accepted by the decision endpoint. Validate against the installed endpoint's actual schema and document supported controls.

Default to one submission per request and one `choice` question covering the full eligibility rubric. Do not concatenate six submissions into one state or ask many questions unnecessarily: each decision question may reprocess the full state. Python attaches the request's stable record ID and stage to the validated response; the model need not generate them.

Listing labels:

- `select`: visible evidence reasonably supports all requested inclusion conditions, without an explicit exclusion.
- `reject`: visible evidence shows a clear nonmatch or explicit exclusion.
- `needs_detail`: evidence is insufficient, a submission detail link exists, and its description may resolve the decision.
- `uncertain`: evidence is insufficient and no usable detail link exists.

Final/detail labels:

- `select`: the combined listing/detail evidence reasonably supports inclusion.
- `reject`: it supports a nonmatch or exclusion.
- `uncertain`: unresolved after available visible-page evidence.

The rubric must define these outcomes for the current user instructions, not just generic relevance. If stakeholder category is not requested, do not make it a hidden requirement. If no answer fits, `uncertain` must be available. Evaluate at submission level; use a separate file-level decision only when instructions actually distinguish attachments.

Validate the expected question key, allowed chosen label, finite probabilities, model identity where returned and response completeness. Map `uncertain` to `review/skipped_uncertain` and continue. Map `needs_detail` to deterministic detail fetching. A listing-selected record still needs its detail page if that is where the file links are, but does not need another relevance call solely because a file URL was discovered. Required links absent after verified detail extraction become `no_attachment`.

Store returned probabilities as diagnostics, not as a verified chance of correctness. Initially route by the chosen label rather than introducing arbitrary probability thresholds. Any later threshold must be tested on labelled samples. Do not require Tev1 to write reasons. Python can log short decision labels such as 'Selected from listing evidence' or 'Skipped: insufficient visible evidence', identifying them as system summaries. Store the source fields used so users can inspect the evidence. Optional detailed explanations belong to Qwen-only mode; no additional Qwen call per Tev1 decision.

### 5.5 Context management and long descriptions

Tev1's documented decision context is approximately 2000 tokens; verify the installed implementation and budget for the entire formatted state, questions, option descriptions and server prompt overhead. This is a different budget from Qwen's 4096-token planner context. Make a conservative client input budget configurable, initially 1400 tokens for state plus questions before reserved formatting overhead, and reduce it if measurements/errors require it. Do not assume changing a chat context setting enlarges the decision model's tested context.

State contains title/subject, relevant submitter/affiliation evidence, visible description, attachment labels/filenames, stage and detail availability. Avoid irrelevant URLs, whole HTML, binary files and accumulated conversation history. Use a compatible tokenizer when practical; otherwise apply a conservative estimate and account for JSON/template overhead. Track actual usage where returned. Never accept silent server truncation.

For a long detail description, extract the main submission text deterministically, preserving title, affiliation and attachment labels. If it still exceeds the budget, split into a bounded number of overlapping text windows and classify those, carrying essential metadata into each window. Default maximum four windows. Treat window disagreement or a reached window cap as uncertainty: do not reject because an inspected fragment lacks relevant evidence. Accept only when the evidence supports all required conditions together and other inspected fragments do not contain contradictory/excluding evidence; otherwise record uncertainty. Explicitly log omitted/partial evidence. Do not use a generated summary that can silently erase exclusions. Do not repeatedly reload Qwen to summarise long records by default.

Do not download attachment bodies, extract PDF text or perform OCR to decide. Best-effort coverage from visible-page metadata is the product goal. Longer contexts, multilingual performance and classification accuracy must be measured on our source examples rather than assumed.

### 5.6 Qwen-only fallback, errors and caching

In `qwen` mode, keep the same rubric, stages and allowed labels; use short schema-constrained `/api/chat` outputs, thinking disabled and one submission per request initially. Python supplies stable IDs. Qwen-only mode does not perform model handoffs. Optional explanations must be brief, evidence-based and bounded.

Retry invalid outputs once, then record an explicit classifier error/uncertainty. Ollama unavailability must never turn unclassified records into rejects or a successful complete run. Memory or endpoint incompatibility is a visible recoverable failure with Qwen-only available as an explicit alternative.

Cache decisions using stage, all inspected evidence/window configuration, rubric, actual backend/model digest and prompt/schema version. Cache listing and final decisions separately. Backend/model changes invalidate the appropriate decision cache. Repeated pages/files must not trigger repeated inference. Preserve historical run decisions. Confirmed absence of attachments and explicit deterministic user rules may avoid inference; keyword absence alone must not reject a submission.

Measure planner calls, decision calls, classifier versus planner time, model loading/handoff time, cache hits, available input/output token counts, peak GPU memory when measurable and total runtime. Model download size is not a VRAM measurement. Do not claim a speedup until compared with the Qwen-only baseline.

## 6. Downloads, persistence and recovery

Use SQLite to persist projects, jobs, pages, extraction plans, records, staged decisions, attachment states, limiter cooldowns and errors. Save checkpoints incrementally. A restarted app must show interrupted jobs and allow explicit Resume. Resume pending work without repeating valid classifications, detail fetches or completed downloads. Support cooperative cancellation between requests; never present a cancelled or interrupted run as complete.

The root download directory is configured once. Each project stores a name, stable ID, source URL and instructions. Create a stable safe directory such as `downloads/eu-csrd-feedback--a1b2c3/`, containing `files/` and `runs/<run_id>/manifest.csv`, `manifest.json` and the instruction/configuration snapshot. Name collisions get different stable IDs. Re-running an existing project uses the same project directory and reuses valid files. Renaming its display name must not break existing paths. Explicitly selecting an existing project resumes/re-runs it; entering an identical name must not silently merge unrelated projects. Sanitise project names and prevent traversal. Every project's downloaded files must be available inside its own directory even if content was reused from another project. Copies or supported hard links are acceptable; manifests must not depend solely on another project's folder. Preserve historical run decisions when instructions change.

Stream downloads to `.part` files with size limits and timeouts, then atomically rename on success. Preserve the original basename where safe, add a stable identifier to avoid collisions, prevent path traversal, and never overwrite unrelated files. Verify that a purported attachment is not an HTML error/login page; use response metadata and basic content inspection rather than extension alone. Retain PDF, DOCX and other actual uploaded document formats.

Deduplicate by canonical attachment URL before fetching and SHA-256 after fetching. Preserve separate submission provenance even when several rows reference identical content. Reuse verified files within the project, with stable per-run manifest references. If URL validity/content changes, do not assume it is the old file; use available validators and record revalidation. Do not unpack archives or execute downloaded content.

Export CSV and JSON manifests containing every discovered row, decisions/reasons, source URLs, attachment URLs and names, page information, local file references, timestamps, HTTP/download status, byte counts, hashes, decision source, and errors. Explain when a download is reused. Distinguish selected submissions, selected attachments, successful downloads and unique files.

## 7. Single-page UI

Make a clean, functional page with no frontend build step:

- Project name, starting URL and multiline custom instructions covering selection and optional navigation hints.
- A compact saved-project selector that loads its URL/instructions and shows its output subdirectory.
- Start, Cancel and Resume controls; one active job at a time for the MVP.
- A small Ollama/model connectivity indicator showing planner/classifier model, active backend and model handoff/loading state.
- A compact advanced mode selector for sequential Qwen-plus-Tev1 versus Qwen-only; changing it applies to a new/resumed run with correct cache invalidation.
- Current stage and counts for listing pages visited, detail pages visited, submissions found, selected, rejected, skipped/uncertain, downloaded, failed and reused; current throttling/cooldown state.
- A recent event log and results table with submitter, subject, filenames, decision, reason and download status.
- Simple filters, manual Include/Exclude for Review rows, and a Download approved rows action.
- CSV/JSON manifest links and safe local download links.

Use lightweight polling or server-sent events. Start downloads automatically as selected attachments are discovered; do not wait for all pages to finish. Uncertain/skipped items never block completion. Manual inclusion can fetch a known detail page to discover its attachments before downloading. Manual exclusion does not silently delete an already downloaded file; flag it in the manifest. Keep project/input/job state after refresh. Show indeterminate progress until totals are known. Completion reports best-effort results and skipped/error counts, not exhaustive coverage.

Treat page and model text as plain text, escape it in the UI, and constrain local-file serving to configured output directories. Default to localhost binding. Apply public HTTP(S)-URL validation to crawled pages and downloads, including redirects; block private/loopback/link-local destinations. The separately configured Ollama endpoint is an intentional internal-service exception.

## 8. Docker and configuration

Deliver a Dockerfile, Compose file, `.env.example`, a usable `.env` with placeholders clearly marked, dependency file, README and all source code. Do not put secrets into images. Persist downloads and SQLite through mounted host directories. Run one application worker so job scheduling is not duplicated. Use a non-root container where practical and document bind-mount permissions.

Provide configuration for at least:

```dotenv
UI_BIND_ADDRESS=127.0.0.1
UI_PORT=8080
HOST_DOWNLOAD_DIR=./downloads
HOST_DATA_DIR=./data
OLLAMA_BASE_URL=http://agent_ollama:11434
OLLAMA_NETWORK=REPLACE_WITH_EXISTING_NETWORK
PLANNER_MODEL=qwen3:14b
CLASSIFIER_BACKEND=decision
CLASSIFIER_MODEL=tev1:4b
QWEN_ONLY_MODEL=qwen3:14b
MODEL_RESIDENCY=sequential
PLANNER_CONTEXT_TOKENS=4096
PLANNER_MAX_OUTPUT_TOKENS=1024
PLANNER_THINKING=false
CLASSIFIER_INPUT_BUDGET_TOKENS=1400
CLASSIFIER_KEEP_ALIVE=10m
MAX_DESCRIPTION_WINDOWS=4
OLLAMA_TIMEOUT_SECONDS=180
MODEL_HANDOFF_TIMEOUT_SECONDS=60
MAX_INFERENCE_CONCURRENCY=1
HTTP_TIMEOUT_SECONDS=30
REQUEST_INTERVAL_SECONDS=2
REQUEST_JITTER_SECONDS=1
PER_ORIGIN_CONCURRENCY=1
GLOBAL_HTTP_CONCURRENCY=2
MAX_HTTP_RETRIES=3
BACKOFF_INITIAL_SECONDS=15
BACKOFF_MAX_SECONDS=300
MAX_PAGES=500
MAX_FILE_MB=100
MAX_DETAIL_DEPTH=1
ENABLE_BROWSER_RENDERING=false
```

Make host paths distinct from container paths, for example mapping HOST_DOWNLOAD_DIR to `/app/downloads` and HOST_DATA_DIR to `/app/data`. Map the configured host port to a fixed internal app port. Join a suitable existing user-defined network shared with `agent_ollama`, using an external Compose network. Do not assume container localhost reaches Ollama. If no suitable network exists, explain the minimal network setup required and do not disrupt the existing container. Document an alternative endpoint via a published host port where applicable. Do not mount the Docker socket in the application.

Validate configuration at startup. The UI should remain available when Ollama is down and explain the fault. Hitting a configured crawl/file limit must produce a visible partial-run status with a reason. Environment variables must actually control behaviour; document units and defaults.

## 9. Implementation and acceptance

First inspect the workspace and existing Ollama configuration, verify the live CARB structure, and then implement end to end. Ask targeted questions only for a real blocker that cannot be resolved through inspection or configuration. Do not stop after scaffolding.

Include meaningful fixture-based tests for table parsing, list/card template discovery, a paginated EU-style list with descriptions and detail pages whose file links are near the bottom, direct files, opaque file URLs, multiple/missing attachments, pagination/filter preservation/repeated pages, schema/unknown-ID validation, cache invalidation and restart/resume. Test that clear nonmatches never fetch detail pages, selected listings with no files do fetch them, uncertain listings get a detail-based decision, and rejected/uncertain final records never download attachment bodies. Test project path safety/name collisions/re-runs and deduplicated provenance. Test shared limiter concurrency, HTTP-date/delta Retry-After, cooldown persistence, retry caps and cancellation during cooldown using mocked clocks. Add mocked tests for planner unload before classifier load, classifier unload before template repair, bounded handoff failure, interrupted handoff/resume, single inference concurrency, cached-plan reuse, backend cache invalidation, decision-response validation, endpoint incompatibility and oversized/contradictory text-window handling. Verify that Tev1 is never asked for free-text explanations and no attachment bodies are fetched for selection. Separate deterministic tests from live-model quality checks.

Run a small end-to-end CARB smoke test locally with a page limit before a full crawl. Manually compare the first fetched table with extracted records and attachment URLs. Inspect a small sample of model decisions, including uncertain cases, and confirm actual downloaded documents open and are not error pages. Record model, settings, sample size, token totals, runtime, and observed limitations; do not claim perfect classification accuracy. Build and run the container and verify the UI and mounted output paths. If the live site or Ollama is unavailable, complete fixture tests and clearly identify which live checks remain unverified.

Acceptance criteria:

1. The app accepts project name, URL and custom selection/navigation instructions, handles both paginated tables and list-to-detail-to-file flows, and downloads selected attachments into the project's directory.
2. Qwen plans and discovers templates; Tev1 receives one compact submission per decision request; Python performs routine navigation and downloads. Sequential mode never depends on simultaneous GPU residency.
3. Decisions use visible information before attachment download; unresolved cases are skipped visibly without blocking completion.
4. Cancellation, resume, caching and deduplication work without losing provenance.
5. Downloads and complete manifests persist outside the container.
6. A new ordinary table/list source can use instructions and bounded discovery without new code; unsupported sources fail transparently and may need an adapter or optional rendering support.
7. The README gives exact setup/start/test commands, authorised Tev1 pull instructions, version requirements, sequential model handoff behaviour, Qwen-only mode and configuration examples.
8. All public HTTP operations share the limiter, obey cooldowns and stop on blocking; rate limits are not presented as a guarantee against blacklisting.

Before choosing a production classifier, compare Qwen-only and Tev1 on 50–100 manually labelled representative listing/detail submissions if the available source permits. Include clear matches/nonmatches, ambiguous affiliations, joint SB253/SB261, generic filenames and long descriptions; include non-English records when the actual project requires them. Reuse the same cached page evidence so network throttling does not distort inference comparisons. Report precision/recall, false negatives, uncertainty rate, inference latency and total model-load/handoff time. Optimise for useful recall, but do not fabricate ground-truth labels or claim independent human evaluation when the agent assigned them. If human labels are unavailable, provide a small inspectable sample and report measured latency/agreement only. Keep Qwen-only available if Tev1 is not better for this data. Verify loaded residency with `nvidia-smi` and `docker exec agent_ollama ollama ps`; identify CPU offload rather than hiding it.

Finish with a concise delivery report: files created, startup commands, UI URL, output location, tests performed, live validation results and remaining limitations.

## Reference documentation

Verify compatibility against the locally installed versions:

- Ollama structured outputs: https://docs.ollama.com/capabilities/structured-outputs
- Ollama chat API: https://docs.ollama.com/api/chat
- Ollama decision API: https://docs.ollama.com/capabilities/decision
- Tev1 4B compatibility/context: https://ollama.com/library/tev1:4b
- Ollama unloading and residency: https://docs.ollama.com/faq
- Docker Compose networking: https://docs.docker.com/compose/how-tos/networking/
- CARB target: https://ww2.arb.ca.gov/approved-comments?entity_id=41096&page=0

The CARB column layout is supplied by the user and must be verified locally. No live DOM selectors or attachment URL patterns are asserted here.
