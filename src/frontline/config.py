"""Settings, read from the environment with sane local defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    vertical: str
    db_path: Path
    audio_dir: Path
    taxonomy_dir: Path

    gcp_project: str | None
    gcs_audio_bucket: str | None
    gcp_region: str

    llm_provider: str
    gemini_model: str

    asr_provider: str

    @property
    def taxonomy_file(self) -> Path:
        return self.taxonomy_dir / f"{self.vertical}.yaml"


def load_settings() -> Settings:
    return Settings(
        vertical=os.getenv("FRONTLINE_VERTICAL", "ev_two_wheeler"),
        db_path=Path(os.getenv("FRONTLINE_DB", REPO_ROOT / "data" / "frontline.db")),
        audio_dir=Path(os.getenv("FRONTLINE_AUDIO_DIR", REPO_ROOT / "data" / "audio")),
        taxonomy_dir=Path(os.getenv("FRONTLINE_TAXONOMY_DIR", REPO_ROOT / "taxonomy")),
        gcp_project=os.getenv("GCP_PROJECT"),
        gcs_audio_bucket=os.getenv("GCS_AUDIO_BUCKET"),
        gcp_region=os.getenv("GCP_REGION", "asia-south1"),
        llm_provider=os.getenv("FRONTLINE_LLM", "gemini"),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        asr_provider=os.getenv("FRONTLINE_ASR", "stub"),
    )


settings = load_settings()
