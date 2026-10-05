import pytest
from config import ConfigError, load_config

KEYS = ["SCADS_API_KEY", "SCADS_BASE_URL", "SCADS_STT_MODEL", "SCADS_LLM_MODEL", "SCADS_GLOSSARY_MODEL"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)


def test_defaults_and_glossary_falls_back_to_llm_model(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=k\nSCADS_LLM_MODEL=my-llm\n")
    cfg = load_config(env)
    assert cfg.api_key == "k"
    assert cfg.base_url == "https://llm.scads.ai/v1"
    assert cfg.stt_model == "alias-stt"
    assert cfg.llm_model == "my-llm"
    assert cfg.glossary_model == "my-llm"


def test_glossary_model_can_differ(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=k\nSCADS_GLOSSARY_MODEL=big\n")
    assert load_config(env).glossary_model == "big"
    assert load_config(env).llm_model == "alias-huge-no-thinking"


def test_real_environment_overrides_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SCADS_API_KEY=from-file\n")
    monkeypatch.setenv("SCADS_API_KEY", "from-env")
    assert load_config(env).api_key == "from-env"


def test_missing_key_raises_without_leaking(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SCADS_BASE_URL=x\n")
    with pytest.raises(ConfigError, match="SCADS_API_KEY"):
        load_config(env)
