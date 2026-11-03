"""Validated API settings; provider credentials follow their feature tasks."""

import ipaddress
import re
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.exceptions import SettingsError


class ConfigurationError(ValueError):
    """Safe configuration failure without input values or file contents."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=None,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    env: Literal["development", "test", "production"]
    host: str
    port: int = Field(ge=1, le=65535)
    max_request_body_bytes: int = Field(default=1048576, ge=1, le=16777216)
    request_body_timeout_seconds: int = Field(default=10, ge=1, le=60)
    database_enabled: bool = False
    database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL", repr=False)

    @field_validator("database_enabled", mode="before")
    @classmethod
    def validate_database_enabled(cls, value: object) -> bool:
        if type(value) is bool:
            return value
        if value in ("true", "false"):
            return value == "true"
        raise ValueError("Use true or false.")

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        # IP literals keep the bind address predictable and reject URLs/credentials.
        if "%" in value:
            raise ValueError("Scoped addresses are not supported.")
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            raise ValueError("An IP address is required.") from None

    @field_validator("port", "max_request_body_bytes", "request_body_timeout_seconds", mode="before")
    @classmethod
    def validate_port(cls, value: object) -> object:
        if type(value) is int:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
            return int(value)
        raise ValueError("An integer setting is required.")


class DatabaseProbeSettings(Settings):
    # Loaded only by the explicitly invoked database probe, never by API startup.
    database_url: SecretStr = Field(validation_alias="DATABASE_URL", repr=False)
    database_direct_url: SecretStr = Field(validation_alias="DATABASE_DIRECT_URL", repr=False)


class MigrationSettings(Settings):
    # Migration needs only the direct URL, not the runtime credential.
    database_direct_url: SecretStr = Field(validation_alias="DATABASE_DIRECT_URL", repr=False)


class OidcProbeSettings(BaseSettings):
    """Explicit probe only; these fields are not loaded by API startup."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True, hide_input_in_errors=True)
    issuer: str = Field(validation_alias="OIDC_ISSUER")
    client_id: str = Field(validation_alias="OIDC_CLIENT_ID")
    client_secret: SecretStr = Field(validation_alias="OIDC_CLIENT_SECRET", repr=False)
    redirect_uri: str = Field(validation_alias="OIDC_REDIRECT_URI")
    public_origin: str = Field(validation_alias="PUBLIC_ORIGIN")
    authorization_endpoint: str = Field(validation_alias="OIDC_AUTHORIZATION_ENDPOINT")
    token_endpoint: str = Field(validation_alias="OIDC_TOKEN_ENDPOINT")
    jwks_uri: str = Field(validation_alias="OIDC_JWKS_URI")


def load_oidc_probe_settings(path: Path) -> OidcProbeSettings:
    try:
        if not path.is_file():
            raise ConfigurationError("OIDC probe configuration unavailable.")
        return OidcProbeSettings(_env_file=path, _env_file_encoding="utf-8")
    except (OSError, UnicodeError, ValueError, SettingsError):
        raise ConfigurationError("OIDC probe configuration unavailable.") from None


def load_settings(
    env_file: Path | None = Path(".env"), *, require_file: bool = False,
    settings_type: type[Settings] = Settings,
) -> Settings:
    """Load only the specified file (if present); process environment wins.

    No parent-directory discovery or environment mutation is performed.
    Explicit --env-file paths must exist; the default .env is optional.
    """
    try:
        if env_file is not None:
            if not env_file.is_file():
                if require_file or env_file.exists():
                    raise ConfigurationError("Configuration file is unavailable.")
                env_file = None
        return settings_type(_env_file=env_file, _env_file_encoding="utf-8")
    except ValidationError as exc:
        fields = sorted(
            {
                ("" if str(error["loc"][0]).upper() in {"DATABASE_URL", "DATABASE_DIRECT_URL"} else "APP_")
                + str(error["loc"][0]).upper()
                for error in exc.errors(include_input=False, include_context=False)
                if error["loc"] and (error["loc"][0] in settings_type.model_fields
                                     or str(error["loc"][0]).startswith("DATABASE_"))
            }
        )
        suffix = ": " + ", ".join(fields) if fields else "."
        raise ConfigurationError("Missing or invalid configuration" + suffix) from None
    except (OSError, UnicodeError, SettingsError):
        raise ConfigurationError("Configuration could not be loaded.") from None
