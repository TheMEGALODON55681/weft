"""Typed settings. Precedence: init args, WEFT_* env vars, TOML file, defaults.

The TOML path comes from WEFT_CONFIG (the CLI's --config sets it), falling back to
./weft.toml when that exists. Unknown keys are rejected so a typo fails loudly.
"""

import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from weft import __version__

# printable ASCII with no space, which is what fits in an `Authorization: Bearer` header
_TOKEN = re.compile(r"[\x21-\x7e]{16,}")


def _is_loopback_host(host: str) -> bool:
    """True for `localhost` and the loopback addresses. Anything else, a name included, is
    treated as reachable from elsewhere, so a doubtful value fails closed."""
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
    # Sub-resources of one render (scripts, images, XHR) share a byte budget, counted as it
    # arrives, and new connections to one host are spaced by render_host_interval_s.
    render_max_bytes: int = Field(default=20_000_000, gt=0)
    render_host_interval_s: float = Field(default=0.05, ge=0)

    # Page cache. A cached page is served without contacting the site until it is
    # cache_ttl_s old, then revalidated with If-None-Match / If-Modified-Since. 0 turns it off.
    cache_ttl_s: float = Field(default=3600.0, ge=0)

    # Crawl jobs. A job stops after crawl_timeout_s of running time, summed across resumes.
    # Each crawl fetch gets crawl_fetch_timeout_s in place of total_timeout_s, so a long
    # Crawl-delay is waited out instead of refused. At most crawl_max_jobs run at once; the rest
    # queue.
    crawl_timeout_s: float = Field(default=1800.0, gt=0)
    crawl_fetch_timeout_s: float = Field(default=120.0, gt=0)
    crawl_concurrency: int = Field(default=4, gt=0)
    crawl_max_jobs: int = Field(default=2, gt=0)

    # Per-host limiter and breaker state is kept for this many hosts, least recently used first
    # out. Hosts that are mid-request, still owed spacing or have an open breaker are kept.
    max_tracked_hosts: int = Field(default=4096, gt=0)

    # Optional model providers, both off. Each flag needs its endpoint and model. An endpoint
    # speaks the chat-completions and embeddings HTTP API that most hosted and local model servers
    # offer. It is set here and nowhere else, so an agent cannot choose it, and calls to it do not
    # go through the SSRF guard. Text from crawled pages is sent there when a feature is on, which
    # is why both are off by default. Keys are best kept in the environment, not in a file.
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key: SecretStr | None = None
    embedding_rerank: bool = False  # rerank search_in_site hits by meaning, after BM25
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    llm_extraction: bool = False  # extract falls back to a model when the rules find too little
    llm_max_input_chars: int = Field(default=30_000, gt=0)  # of page text sent to the model
    provider_timeout_s: float = Field(default=60.0, gt=0)

    # The transport. stdio is for a client that launches Weft. http serves streamable HTTP at /mcp
    # for remote connectors, with /healthz beside it. Bound beyond loopback, http needs a bearer
    # token, and the server refuses to start without one.
    transport: Literal["stdio", "http"] = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8000, ge=1, le=65535)
    auth_token: SecretStr | None = None

    @field_validator("auth_token", mode="before")
    @classmethod
    def _usable_token(cls, value: Any) -> Any:
        """An empty token is no token: an unset variable in a compose file or a secret mount
        arrives as an empty string and must not become a password. The message never holds the
        value."""
        if isinstance(value, SecretStr):  # settings built in code may pass one
            value = value.get_secret_value()
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value:
            return None
        if not _TOKEN.fullmatch(value):
            raise ValueError("must be at least 16 printable ASCII characters with no spaces")
        return value

    @model_validator(mode="after")
    def _remote_needs_a_token(self) -> Self:
        if (
            self.transport == "http"
            and self.auth_token is None
            and not _is_loopback_host(self.http_host)
        ):
            raise ValueError(
                "auth_token is required when http_host is not a loopback address "
                "(set WEFT_AUTH_TOKEN)"
            )
        return self

    @field_validator("embedding_base_url", "llm_base_url")
    @classmethod
    def _http_endpoint(cls, value: str) -> str:
        if value and not value.lower().startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value

    # Declared after the endpoint fields so that `info.data` already holds them.
    @field_validator("embedding_rerank", "llm_extraction")
    @classmethod
    def _needs_an_endpoint(cls, on: bool, info: ValidationInfo) -> bool:
        prefix = "embedding" if info.field_name == "embedding_rerank" else "llm"
        if on and not (info.data.get(f"{prefix}_base_url") and info.data.get(f"{prefix}_model")):
            raise ValueError(f"{info.field_name} needs {prefix}_base_url and {prefix}_model")
        return on

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
