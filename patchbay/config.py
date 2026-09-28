from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # llm (openai-compatible); validated where used
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.2
    llm_thinking: str = ""  # off | low | high | max; "" = the provider's default
    prompt_version: str = "v2"
    demo_delay_s: float = 0.6  # per-step pause of the scripted demo model

    # infra
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "sqlite:///data/patchbay.db"

    # sandbox
    sandbox_image: str = "patchbay-sandbox:latest"
    sandbox_network: str = "bridge"  # or "none"
    sandbox_memory: str = "2g"
    sandbox_cpus: float = 2.0
    sandbox_ttl_minutes: int = 60
    maintenance_interval_s: int = 60  # reaper + sweeper cadence (celery beat)
    lease_ttl_s: int = 90  # a live run renews its lease; a dead worker's expires

    # agent limits
    max_steps: int = 40
    context_budget_tokens: int = 0  # compact old tool outputs past this; 0 = off
    context_keep_turns: int = 3  # the newest N turns always keep their tool outputs
    max_session_tokens: int = 400_000  # across runs; cached tokens count a tenth; 0 = off
    verify_rounds: int = 0  # review each finish, rejecting at most N times; 0 = off
    max_tool_output_chars: int = 8000
    default_tool_timeout_s: int = 120
    max_tool_timeout_s: int = 600


@lru_cache
def get_settings() -> Settings:
    return Settings()
