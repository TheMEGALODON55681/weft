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
