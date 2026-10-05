"""Configuration from .env / environment. Real environment variables win over the file."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_ENV = Path(__file__).parent / ".env"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    stt_model: str
    llm_model: str
    glossary_model: str

    def __repr__(self) -> str:  # never expose the key
        return f"Config(base_url={self.base_url!r}, stt_model={self.stt_model!r}, llm_model={self.llm_model!r})"


def load_config(env_path: Path | None = None) -> Config:
    values = {**dotenv_values(env_path or DEFAULT_ENV), **os.environ}
    key = values.get("SCADS_API_KEY")
    if not key:
        raise ConfigError("SCADS_API_KEY is not set (put it in .env, see .env.example)")
    llm = values.get("SCADS_LLM_MODEL") or "alias-huge-no-thinking"
    return Config(
        api_key=key,
        base_url=values.get("SCADS_BASE_URL") or "https://llm.scads.ai/v1",
        stt_model=values.get("SCADS_STT_MODEL") or "alias-stt",
        llm_model=llm,
        glossary_model=values.get("SCADS_GLOSSARY_MODEL") or llm,
    )


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise ConfigError("ffmpeg not found on PATH")
