from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONDUIT_",
        env_file=".env",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8765

    default_model: str = "claude-sonnet-4-6"
    default_system_prompt: str | None = None
    default_effort: EffortLevel | None = None  # None → SDK default
    allowed_tools: list[str] = Field(default_factory=list)

    session_idle_timeout_s: int = 30 * 60
    max_sessions: int = 100


@lru_cache
def settings() -> Settings:
    return Settings()
