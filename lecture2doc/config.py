"""Configuration from .env / environment. Real environment variables win over the file."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values



def default_env() -> Path:
    """.env in the current folder, else the one next to the project checkout."""
    here = Path.cwd() / ".env"
    return here if here.exists() else Path(__file__).resolve().parent.parent / ".env"


AUTO = "auto"  # as llm_model / glossary_model: the default models, the fastest answers each request
DEFAULT_CHAT_MODELS = ("alias-huge-no-thinking", "zai-org/GLM-5.3", "MiniMaxAI/MiniMax-M3", "openai/gpt-oss-120b")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str
    stt_model: str
    llm_model: str
    glossary_model: str
    requests_per_minute: int = 25  # below the gateway's observed 30/min per key and model
    llm_models: tuple[str, ...] = DEFAULT_CHAT_MODELS  # what AUTO chooses from

    def default_models(self, task: str) -> tuple[str, ...]:
        """The models a task uses when the user has not chosen any: what the .env says, else DEFAULT_CHAT_MODELS.
        Tasks: "glossary", "cleanup" (both chat models) and "transcribe" (speech to text)."""
        if task == "transcribe":
            return (self.stt_model,)
        model = self.glossary_model if task == "glossary" else self.llm_model
        return self.llm_models if model == AUTO else (model,)

    def __repr__(self) -> str:  # never expose the key
        return f"Config(base_url={self.base_url!r}, stt_model={self.stt_model!r}, llm_model={self.llm_model!r})"


def load_config(env_path: Path | None = None) -> Config:
    values = {**dotenv_values(env_path or default_env()), **os.environ}
    key = values.get("SCADS_API_KEY")
    if not key:
        raise ConfigError("SCADS_API_KEY is not set (put it in .env, see .env.example)")
    llm = values.get("SCADS_LLM_MODEL") or AUTO
    rpm = values.get("SCADS_REQUESTS_PER_MINUTE") or "25"
    try:
        per_minute = int(rpm)
    except ValueError:
        per_minute = 0
    if per_minute < 1:
        raise ConfigError(f"SCADS_REQUESTS_PER_MINUTE must be a positive whole number, got {rpm!r}")
    return Config(
        api_key=key,
        base_url=values.get("SCADS_BASE_URL") or "https://llm.scads.ai/v1",
        stt_model=values.get("SCADS_STT_MODEL") or "alias-stt",
        llm_model=llm,
        glossary_model=values.get("SCADS_GLOSSARY_MODEL") or llm,
        requests_per_minute=per_minute,
        llm_models=tuple(m.strip() for m in (values.get("SCADS_LLM_MODELS") or "").split(",") if m.strip())
        or DEFAULT_CHAT_MODELS,
    )


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise ConfigError("ffmpeg not found on PATH")
