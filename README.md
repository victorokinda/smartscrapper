# SmartScraper

Local smart document scraper with sequential Qwen & Tev1 Ollama inference. Discovers public document submissions from paginated tables and listing-to-detail portals, evaluates eligibility using local LLMs, and downloads matching attachments with rate limiting, provenance tracking, and persistence.

> 📖 **Authoring Guide for AI Models**: See [`INSTRUCTION_GUIDE.md`](file:///home/vick/smartscraper/INSTRUCTION_GUIDE.md) for how LLMs and prompt engineers should write selection criteria and navigation hints for SmartScraper.

---

## 1. Architecture & Design

- **Orchestration**: Deterministic Python pipeline built on **FastAPI**, **httpx**, and **BeautifulSoup**. The crawl loop, pagination, and downloading are strictly managed in Python—models never control the loop or invent URLs.
- **Two-Tier Sequential Inference**:
  - **Planning Phase**: `qwen3:14b` interprets natural language user instructions into a structured `DecisionRubric` and assists with bounded template discovery.
  - **Sequential GPU Handoff**: `qwen3:14b` is explicitly unloaded from VRAM (`keep_alive=0` verified via `/api/ps`).
  - **Classification Phase**: `tev1:4b` classifies records via Ollama's `/v1/systemone` endpoint using label choices (`select`, `reject`, `needs_detail`, `uncertain`) with confidence diagnostics.
  - **Qwen-Only Baseline**: An optional toggle in the UI and `.env` (`CLASSIFIER_BACKEND=qwen`) allows running solely on Qwen 14B without Tev1.
- **Deterministic Crawling & 2-Stage Decision Pipeline**:
  - Listing evidence is extracted. Clear nonmatches are rejected immediately without visiting detail pages.
  - Selected items with direct files download immediately. Selected items with files on detail pages visit the detail page to discover links.
  - Unresolved cases (`needs_detail`) visit the detail page, extract extended text, and make a final classification.
  - Files are only downloaded after a validated `select` decision. Uncertain cases are tagged `Review` and never block crawl completion.
- **Rate Limiting & Safety**:
  - Shared per-origin rate limiter (2s default interval + jitter, max 1 in-flight per origin, global concurrency cap of 2).
  - Handles `Retry-After` headers (both delta-seconds and HTTP-date).
  - Exponential backoff with jitter on 429/503.
  - Origin blocking on 403 or CAPTCHAs.
  - SSRF protection: blocks private, loopback, and link-local destinations (with NAT64 support).
- **Persistence & Manifests**:
  - Incremental checkpoints in SQLite (`smartscraper.db`).
  - Safe download directory per project (`downloads/<project-slug>--<id>/files/`).
  - Atomic downloads via `.part` files, verified non-HTML content, SHA-256 deduplication, and URL deduplication.
  - Manifest generation in CSV (`manifest.csv`) and JSON (`manifest.json`) preserving full submission provenance.
- **Single-Page UI**:
  - Vanilla HTML/CSS/JavaScript with responsive dark theme and zero frontend build pipeline.
  - Real-time job monitoring, stats counters, event log, filterable results table, manual review actions, and safe file links.

---

## 2. Requirements

- **GPU & Memory**: NVIDIA GPU (16 GB VRAM recommended for sequential 14B/4B residency) or CPU fallback.
- **Ollama**: Version **0.35.0 or later** with `/v1/systemone` support.
- **Docker & Docker Compose**: v2+ with Docker network connectivity.
- **Python**: 3.11+ (if running directly without Docker).

---

## 3. Ollama & Tev1 Setup

Ensure your Ollama container is running (default container name: `agent_ollama` on `shared_ai_net`).

Pull the required models:
```bash
# Pull planner (Qwen 14B)
docker exec agent_ollama ollama pull qwen3:14b

# Pull classifier (Tev1 4B)
docker exec agent_ollama ollama pull tev1:4b
```

Verify Ollama version and `/v1/systemone` availability:
```bash
docker exec agent_ollama ollama --version
# Expected: ollama version is 0.35.0 or higher

curl -s -X POST http://127.0.0.1:11434/v1/systemone -H "Content-Type: application/json" -d '{}'
# Expected response: {"error":"model is required"}
```

---

## 4. Quick Start with Docker Compose

1. Clone or copy into workspace:
```bash
cd /home/vick/smartscraper
```

2. Configure environment:
```bash
cp .env.example .env
```
*(Ensure `OLLAMA_NETWORK=shared_ai_net` and `OLLAMA_BASE_URL=http://agent_ollama:11434` in `.env` for Docker)*

3. Build and launch:
```bash
docker compose up -d --build
```

4. Open the Web UI:
Navigate to [http://127.0.0.1:8080](http://127.0.0.1:8080) in your browser.

5. View logs:
```bash
docker compose logs -f smartscraper
```

---

## 5. Running Locally (Direct Python)

If running outside Docker:
```bash
# 1. Create virtual environment and install dependencies
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# 2. Configure .env for local host
# Set OLLAMA_BASE_URL=http://127.0.0.1:11434 in .env

# 3. Start application
python3 -m uvicorn app.web:app --host 127.0.0.1 --port 8080
```

---

## 6. Running Tests

Run the full automated test suite:
```bash
pytest tests/ -v
```

Run the live CARB smoke test:
```bash
python3 tests/smoke_carb.py
```

---

## 7. Configuration Reference (.env)

| Variable | Default | Description |
|---|---|---|
| `UI_BIND_ADDRESS` | `127.0.0.1` | Local IP address to bind UI |
| `UI_PORT` | `8080` | Port for the web application |
| `HOST_DOWNLOAD_DIR` | `./downloads` | Host volume path for downloaded files and manifests |
| `HOST_DATA_DIR` | `./data` | Host volume path for persistent SQLite database |
| `OLLAMA_BASE_URL` | `http://agent_ollama:11434` | URL of Ollama service |
| `OLLAMA_NETWORK` | `shared_ai_net` | Docker network shared with Ollama |
| `PLANNER_MODEL` | `qwen3:14b` | Model used for rubric planning and template discovery |
| `CLASSIFIER_BACKEND` | `decision` | Active classifier (`decision` for Tev1, `qwen` for Qwen-only) |
| `CLASSIFIER_MODEL` | `tev1:4b` | Model used for SystemOne decision classification |
| `QWEN_ONLY_MODEL` | `qwen3:14b` | Model used for fallback/baseline classification |
| `MODEL_RESIDENCY` | `sequential` | GPU residency mode (`sequential` or `concurrent`) |
| `PLANNER_CONTEXT_TOKENS` | `4096` | Context window tokens for planner |
| `PLANNER_MAX_OUTPUT_TOKENS` | `1024` | Output token cap for planner |
| `PLANNER_THINKING` | `false` | Explicitly disables reasoning/thinking tokens |
| `CLASSIFIER_INPUT_BUDGET_TOKENS` | `1400` | Token budget for classifier state |
| `CLASSIFIER_KEEP_ALIVE` | `10m` | Idle unload timeout for classifier |
| `MAX_DESCRIPTION_WINDOWS` | `4` | Maximum overlapping windows for long submission descriptions |
| `OLLAMA_TIMEOUT_SECONDS` | `180` | Ollama HTTP request timeout |
| `MODEL_HANDOFF_TIMEOUT_SECONDS` | `60` | Maximum wait for model unload confirmation |
| `REQUEST_INTERVAL_SECONDS` | `2` | Minimum interval between HTTP requests per origin |
| `REQUEST_JITTER_SECONDS` | `1` | Random jitter added to request interval |
| `PER_ORIGIN_CONCURRENCY` | `1` | In-flight request limit per origin |
| `GLOBAL_HTTP_CONCURRENCY` | `2` | Maximum total concurrent outbound HTTP requests |
| `MAX_HTTP_RETRIES` | `3` | Maximum retries on 429/503 or transient network failures |
| `BACKOFF_INITIAL_SECONDS` | `15` | Base backoff interval on rate limits |
| `BACKOFF_MAX_SECONDS` | `300` | Maximum exponential backoff ceiling |
| `MAX_PAGES` | `500` | Maximum crawl page limit |
| `MAX_FILE_MB` | `100` | File download size limit |
| `MAX_DETAIL_DEPTH` | `1` | Depth limit for listing-to-detail hops |

---

## 8. Verified CARB Preset Details

Target Starting URL:
`https://ww2.arb.ca.gov/approved-comments?entity_id=41096&page=0`

Verified Live DOM:
- **Columns**: `Name`, `Subject:`, `Submitted to:`, `File Upload (i.e., Attachment):`, `Comment`
- **File Uploads**: Contains direct `<a>` links to PDF and document attachments in Drupal's `/system/files/webform/public_comments/<id>/` path. Some rows contain multiple attachments.
- **Comment Links**: Lead to `/form/public-comments/submissions/<id>` containing administrative metadata.
- **Pagination**: Managed by `<a class="pager__link pager__link--next" href="?entity_id=41096&page=1">`. Query parameter `entity_id=41096` is preserved across all page requests.
