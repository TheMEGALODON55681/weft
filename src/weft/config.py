"""Typed settings. Precedence: init args, WEFT_* env vars, TOML file, defaults.

The TOML path comes from WEFT_CONFIG (the CLI's --config sets it), falling back to
./weft.toml when that exists. Unknown keys are rejected so a typo fails loudly.
"""

import os
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from weft import __version__


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEFT_", extra="forbid")

    data_dir: Path = Path.home() / ".weft"
    # Shown to site operators in the User-Agent so they can reach whoever runs the crawler.
    contact: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    http2: bool = True
    connect_timeout_s: float = Field(default=10.0, gt=0)
    read_timeout_s: float = Field(default=20.0, gt=0)
    # Hard wall-clock ceiling for one fetch, including every redirect hop and the body read.
    total_timeout_s: float = Field(default=30.0, gt=0)
    max_body_bytes: int = Field(default=5_000_000, gt=0)
    max_redirects: int = Field(default=10, ge=0)

    # Caps on what a single fetch_page call hands back to the agent.
    max_links: int = Field(default=200, gt=0)
    max_content_chars: int = Field(default=100_000, gt=0)
    # Decoding and extraction run in a worker thread that cannot be interrupted, so this frees the
    # caller, not the thread.
    extract_timeout_s: float = Field(default=10.0, gt=0)

    # SSRF guard. Private, link-local, metadata and other non-public addresses are always refused.
    # Loopback is refused too unless this is set; leave it off outside local development.
    allow_loopback: bool = False

    # Politeness. Limits are per host name, apart from the global cap. A robots.txt Crawl-delay
    # raises the interval for that host. 0 turns the interval off.
    host_min_interval_s: float = Field(default=1.0, ge=0)
    host_concurrency: int = Field(default=2, gt=0)
    global_concurrency: int = Field(default=16, gt=0)

    # Resilience. Retries cover transport errors and 429/502/503/504 responses. The breaker opens
    # for a host after this many consecutive failures, stays open for 30 s, then lets one probe
    # through.
    max_retries: int = Field(default=2, ge=0)
    retry_backoff_s: float = Field(default=0.5, ge=0)
    breaker_threshold: int = Field(default=5, gt=0)

    # Rendering. render="auto" starts a headless browser only for a page that has scripts and
    # fewer than render_min_words words of content, or asks for JavaScript in a <noscript>. The
    # browser starts on first use and is replaced after browser_recycle_after pages. Each page's
    # JavaScript heap is capped at browser_js_heap_mb; a page that exceeds it is killed.
    render_min_words: int = Field(default=50, ge=0)
    render_timeout_s: float = Field(default=20.0, gt=0)
    browser_max_pages: int = Field(default=2, gt=0)
    browser_recycle_after: int = Field(default=50, gt=0)
    browser_js_heap_mb: int = Field(default=256, ge=16)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "weft.db"

    @property
    def user_agent(self) -> str:
        base = f"weft/{__version__}"
        return f"{base} (contact: {self.contact})" if self.contact else base

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        toml = TomlConfigSettingsSource(
            settings_cls, toml_file=os.environ.get("WEFT_CONFIG", "weft.toml")
        )
        return (init_settings, env_settings, toml)
