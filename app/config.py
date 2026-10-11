from pathlib import Path
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    UI_BIND_ADDRESS: str = "127.0.0.1"
    UI_PORT: int = 8080
    HOST_DOWNLOAD_DIR: str = "./downloads"
    HOST_DATA_DIR: str = "./data"
    DOWNLOAD_DIR: str = "./downloads"
    DATA_DIR: str = "./data"

    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    OLLAMA_NETWORK: str = "shared_ai_net"
    PLANNER_MODEL: str = "qwen3:14b"
    CLASSIFIER_BACKEND: Literal["decision", "qwen"] = "decision"
    CLASSIFIER_MODEL: str = "tev1:4b"
    QWEN_ONLY_MODEL: str = "qwen3:14b"
    MODEL_RESIDENCY: Literal["sequential", "concurrent"] = "sequential"

    PLANNER_CONTEXT_TOKENS: int = 4096
    PLANNER_MAX_OUTPUT_TOKENS: int = 1024
    PLANNER_THINKING: bool = False
    CLASSIFIER_INPUT_BUDGET_TOKENS: int = 1400
    CLASSIFIER_KEEP_ALIVE: str = "10m"
    MAX_DESCRIPTION_WINDOWS: int = 4

    OLLAMA_TIMEOUT_SECONDS: float = 180.0
    MODEL_HANDOFF_TIMEOUT_SECONDS: float = 60.0
    MAX_INFERENCE_CONCURRENCY: int = 1

    HTTP_TIMEOUT_SECONDS: float = 30.0
    REQUEST_INTERVAL_SECONDS: float = 2.0
    REQUEST_JITTER_SECONDS: float = 1.0
    PER_ORIGIN_CONCURRENCY: int = 1
    GLOBAL_HTTP_CONCURRENCY: int = 2
    MAX_HTTP_RETRIES: int = 3
    BACKOFF_INITIAL_SECONDS: float = 15.0
    BACKOFF_MAX_SECONDS: float = 300.0

    MAX_PAGES: int = 500
    MAX_FILE_MB: int = 100
    MAX_DETAIL_DEPTH: int = 1
    ENABLE_BROWSER_RENDERING: bool = True
    PLAYWRIGHT_TIMEOUT_SECONDS: float = 30.0
    PLAYWRIGHT_HEADLESS: bool = True

    @property
    def download_path(self) -> Path:
        p = Path(self.DOWNLOAD_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()

    @property
    def data_path(self) -> Path:
        p = Path(self.DATA_DIR)
        p.mkdir(parents=True, exist_ok=True)
        return p.resolve()

    @property
    def db_path(self) -> Path:
        return self.data_path / "smartscraper.db"


settings = Settings()
