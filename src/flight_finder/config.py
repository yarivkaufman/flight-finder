"""Configuration: config.yaml (rules, schedule) + environment variables (secrets/paths)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class Defaults(BaseModel):
    """Used for a new trip when the request doesn't say otherwise."""

    origin: str = "EWR"
    currency: str = "USD"
    adults: int = 1
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    flex_days: int = 3  # also check this many days earlier/later (same trip length)
    value_per_hour: float = 15.0  # what an hour of travel time is worth (ranking)
    value_per_stop: float = 40.0  # what avoiding one stop is worth (ranking)


class Email(BaseModel):
    # a drop emails when the cheapest price beats the last emailed low by at least this much (either)
    min_drop_usd: float = 25.0
    min_drop_pct: float = 5.0
    weekly_summary_day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "sun"
    weekly_summary_at: str = "08:00"  # HH:MM local
    failing_days: int = 3  # a source failing this many daily checks in a row goes in the summary


class Google(BaseModel):
    # SerpApi plan is shared with price-agent: never use it when fewer searches than this are left
    reserve: int = 60
    return_options: int = 3  # a round trip costs 1 + this many searches


class Parsing(BaseModel):
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"


class Config(BaseModel):
    timezone: str = "America/New_York"
    check_at: str = "07:30"  # HH:MM local: the daily Duffel check of every active trip
    web_base_url: str = "http://localhost:8097"
    results_per_check: int = 15  # offers kept per date pair
    defaults: Defaults = Field(default_factory=Defaults)
    email: Email = Field(default_factory=Email)
    google: Google = Field(default_factory=Google)
    parsing: Parsing = Field(default_factory=Parsing)


class Settings(BaseModel):
    """Runtime settings from the environment."""

    data_dir: Path
    config_path: Path
    anthropic_configured: bool
    serpapi_key: str | None = Field(default=None, repr=False)
    duffel_token: str | None = Field(default=None, repr=False)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = Field(default=None, repr=False)
    email_from: str | None = None
    email_to: list[str] = Field(default_factory=list)
    demo: bool = False  # FLIGHTS_DEMO=1: use offline demo data instead of real sources

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.email_to)

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            data_dir=Path(env("DATA_DIR", "data")),
            config_path=Path(env("CONFIG_PATH", "config.yaml")),
            anthropic_configured=bool(env("ANTHROPIC_API_KEY")),
            serpapi_key=env("SERPAPI_KEY", "").strip() or None,
            duffel_token=env("DUFFEL_ACCESS_TOKEN", "").strip() or None,
            smtp_host=env("SMTP_HOST", "").strip() or None,
            smtp_port=int(env("SMTP_PORT", "587") or 587),
            smtp_user=env("SMTP_USER", "").strip() or None,
            smtp_password=env("SMTP_PASSWORD", "").strip() or None,
            email_from=env("EMAIL_FROM", "").strip() or env("SMTP_USER", "").strip() or None,
            email_to=[a.strip() for a in env("EMAIL_TO", "").split(",") if a.strip()],
            demo=env("FLIGHTS_DEMO", "") in ("1", "true", "yes"),
        )


def load_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    with open(path, encoding="utf-8") as f:
        return Config.model_validate(yaml.safe_load(f) or {})
