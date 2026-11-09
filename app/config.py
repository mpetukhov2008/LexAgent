import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    database: Path = ROOT / "data/prospects.sqlite3"
    api_key: str = field(default="", repr=False)
    folder_id: str = ""
    model_uri: str = ""
    enable_yandex: bool = False
    enable_rule_critic: bool = False
    database_url: str = field(default="", repr=False)
    gigachat_auth_key: str = field(default="", repr=False)
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2-Pro"
    parser_api_key: str = field(default="", repr=False)
    worker_enabled: bool = True
    poll_seconds: float = 0.3
    lease_seconds: int = 30
    max_attempts: int = 3
    max_steps: int = 40
    max_model_calls: int = 20
    run_timeout: int = 600
    total_model_calls: int = 80
    search_queries: int = 3
    research_acts: int = 8
    enable_tool_research: bool = True
    enable_semantic_critic: bool = True
    source_max_calls: int = 40
    source_max_pages: int = 3
    source_attempts: int = 3
    source_backoff: float = 0.5
    history_max_acts: int = 100
    history_max_pages: int = 10
    document_dir: Path = ROOT / "data/originals"
    enable_ocr: bool = True
    tesseract_cmd: str = "tesseract"
    ocr_languages: str = "rus+eng"
    pdf_timeout: int = 180
    ocr_page_timeout: int = 20

    @property
    def yandex_ready(self):
        return bool(self.enable_yandex and self.api_key and self.folder_id)

    @property
    def model(self):
        return self.model_uri or f"gpt://{self.folder_id}/aliceai-llm"

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        path = Path(os.getenv("DATABASE_PATH", "data/prospects.sqlite3"))
        documents = Path(os.getenv("DOCUMENT_DIR") or str(ROOT / "data/originals"))
        return cls(
            database_url=os.getenv("DATABASE_URL", ""),
            gigachat_auth_key=os.getenv("GIGACHAT_AUTH_KEY", ""),
            gigachat_scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
            gigachat_model=os.getenv("GIGACHAT_MODEL", "GigaChat-2-Pro"),
            parser_api_key=os.getenv("PARSER_API_KEY", ""),
            enable_tool_research=os.getenv("ENABLE_TOOL_RESEARCH", "true").lower() == "true",
            enable_semantic_critic=os.getenv("ENABLE_SEMANTIC_CRITIC", "true").lower() == "true",
            document_dir=documents if documents.is_absolute() else ROOT / documents,
            enable_ocr=os.getenv("ENABLE_OCR", "true").lower() == "true",
            tesseract_cmd=os.getenv("TESSERACT_CMD", "tesseract"),
            ocr_languages=os.getenv("OCR_LANGUAGES", "rus+eng"),
            database=path if path.is_absolute() else ROOT / path,
            api_key=os.getenv("YANDEX_API_KEY", "").strip(),
            folder_id=os.getenv("YANDEX_FOLDER_ID", "").strip(),
            model_uri=os.getenv("YANDEX_MODEL_URI", "").strip(),
            enable_yandex=os.getenv("ENABLE_YANDEX", "false").lower() == "true",
            enable_rule_critic=os.getenv("ENABLE_RULE_CRITIC", "false").lower() == "true",
        )
