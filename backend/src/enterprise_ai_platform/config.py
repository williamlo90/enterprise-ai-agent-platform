"""Validated API settings; provider credentials follow their feature tasks."""

import ipaddress
import base64
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
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
    database_schema: str = Field(default='platform', pattern=r'^(?:platform|db03_probe_[a-f0-9]{32})$')
    browser_context_enabled: bool = False
    database_pool_max_size: int = Field(default=2, ge=1, le=2)
    database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL", repr=False)
    conversations_enabled: bool = False
    knowledge_enabled: bool = False
    organization_enabled: bool = False
    administration_enabled: bool = False
    privacy_enabled: bool = False
    files_enabled: bool = False
    files_upload_enabled: bool = True
    receipts_enabled: bool = False
    drafts_enabled: bool = False
    actions_enabled: bool = False
    flow_enabled: bool = False
    reimbursement_recovery_credential: SecretStr | None = Field(default=None,repr=False)
    receipt_security: SecretStr | None = Field(default=None,repr=False)
    files_bucket: str | None = None
    internal_auth_enabled: bool = False
    delegation_issuer: str = "enterprise-ai-platform"
    reimbursement_enabled: bool = False
    reimbursement_origin: str | None = None
    reimbursement_dispatcher_credential: SecretStr | None = Field(default=None,repr=False)
    reimbursement_signing_private_key: SecretStr | None = Field(default=None,repr=False)
    reimbursement_signing_kid: str = 'current'
    reimbursement_settlement_key_id: str = 'reference-v1'
    auth_enabled: bool = False
    auth_callback_logging_controlled: bool = False
    auth_jwks_background_refresh: bool = True
    auth_demo_ui: bool = False
    auth_verifier_key: SecretStr | None = Field(default=None, repr=False)
    oidc_issuer: str | None = Field(default=None, validation_alias="OIDC_ISSUER")
    oidc_client_id: str | None = Field(default=None, validation_alias="OIDC_CLIENT_ID")
    oidc_client_secret: SecretStr | None = Field(default=None, validation_alias="OIDC_CLIENT_SECRET", repr=False)
    oidc_redirect_uri: str | None = Field(default=None, validation_alias="OIDC_REDIRECT_URI")
    public_origin: str | None = Field(default=None, validation_alias="PUBLIC_ORIGIN")
    oidc_authorization_endpoint: str | None = Field(default=None, validation_alias="OIDC_AUTHORIZATION_ENDPOINT")
    oidc_token_endpoint: str | None = Field(default=None, validation_alias="OIDC_TOKEN_ENDPOINT")
    oidc_jwks_uri: str | None = Field(default=None, validation_alias="OIDC_JWKS_URI")

    @field_validator("privacy_enabled", "administration_enabled", "flow_enabled", "actions_enabled", "drafts_enabled", "receipts_enabled", "reimbursement_enabled", "browser_context_enabled", "conversations_enabled", "knowledge_enabled", "organization_enabled", "files_enabled", "internal_auth_enabled", "database_enabled", "auth_enabled", "auth_callback_logging_controlled", "auth_jwks_background_refresh", "auth_demo_ui", mode="before")
    @classmethod
    def validate_database_enabled(cls, value: object) -> bool:
        if type(value) is bool:
            return value
        if value in ("true", "false"):
            return value == "true"
        raise ValueError("Use true or false.")

    @model_validator(mode="after")
    def validate_auth(self):
        if self.privacy_enabled and not (self.auth_enabled and self.database_enabled and self.browser_context_enabled):raise ValueError('Privacy requires identity, database and browser context.')
        if self.administration_enabled and not (self.auth_enabled and self.database_enabled and self.browser_context_enabled):raise ValueError('Administration requires identity, database and browser context.')
        if self.flow_enabled:
            if not self.actions_enabled or self.reimbursement_recovery_credential is None:raise ValueError('Flow requires actions and private recovery configuration.')
            recovery=self.reimbursement_recovery_credential.get_secret_value()
            if not re.fullmatch('[A-Za-z0-9_-]{43}',recovery) or (self.reimbursement_dispatcher_credential is not None and recovery==self.reimbursement_dispatcher_credential.get_secret_value()):raise ValueError('Invalid recovery credential.')
        if self.actions_enabled and not self.drafts_enabled:raise ValueError('Actions require configured draft and receipt services.')
        if self.drafts_enabled and not self.receipts_enabled:raise ValueError('Drafts require configured receipt and reimbursement services.')
        if self.receipts_enabled and (not self.reimbursement_enabled or not self.files_enabled or self.receipt_security is None):
            raise ValueError('Receipts require private reimbursement integration, files and operator security configuration.')
        if self.reimbursement_enabled:
            try:
                if not (self.database_enabled and self.auth_enabled and self.internal_auth_enabled and self.knowledge_enabled and self.browser_context_enabled):raise ValueError
                target=urlsplit(self.reimbursement_origin or '')
                if re.search(r'[\x00-\x20\x7f]',self.reimbursement_origin or ''):raise ValueError
                if target.scheme!='https' or not target.hostname or target.username or target.password or target.path or target.query or target.fragment:raise ValueError
                if target.port is not None and not 1<=target.port<=65535:raise ValueError
                if self.reimbursement_dispatcher_credential is None or not re.fullmatch('[A-Za-z0-9_-]{43}',self.reimbursement_dispatcher_credential.get_secret_value()):raise ValueError
                if self.reimbursement_signing_private_key is None or not 1<=len(self.reimbursement_signing_private_key.get_secret_value())<=16384:raise ValueError
                if any(not re.fullmatch('[A-Za-z0-9_.-]{1,64}',v) for v in (self.reimbursement_signing_kid,self.reimbursement_settlement_key_id)):raise ValueError
            except (ValueError,TypeError):raise ValueError('Reimbursement requires complete private integration, auth, knowledge, database and browser context configuration.') from None
        if self.browser_context_enabled and (not self.auth_enabled or not self.database_enabled):
            raise ValueError('Browser context requires authentication and database.')
        if self.conversations_enabled and (not self.auth_enabled or not self.database_enabled):
            raise ValueError('Conversations require authentication and database.')
        if self.knowledge_enabled and (not self.auth_enabled or not self.database_enabled):
            raise ValueError('Knowledge requires authentication and database.')
        if self.organization_enabled and (not self.auth_enabled or not self.database_enabled):
            raise ValueError('Organization requires authentication and database.')
        if self.files_enabled and self.files_upload_enabled and (os.environ.get('AWS_LAMBDA_FUNCTION_NAME') or os.environ.get('VERCEL')=='1'):
            raise ValueError('File upload transport requires a host supporting the full byte limit.')
        if self.files_enabled and (not self.auth_enabled or not self.database_enabled or not self.files_bucket or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',self.files_bucket)):
            raise ValueError('Private file configuration is invalid.')
        if self.internal_auth_enabled and (not self.database_enabled or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}",self.delegation_issuer)):
            raise ValueError("Internal verification configuration is invalid.")
        if self.auth_enabled:
            if os.environ.get("VERCEL") == "1":
                raise ValueError("Authentication callback logging is not approved on this host.")
            if not self.database_enabled or not self.auth_callback_logging_controlled:
                raise ValueError("Authentication requires database and controlled callback logging.")
            if any(not getattr(self, name) for name in ("auth_verifier_key", "oidc_issuer", "oidc_client_id",
                    "oidc_client_secret", "oidc_redirect_uri", "public_origin", "oidc_authorization_endpoint",
                    "oidc_token_endpoint", "oidc_jwks_uri")):
                raise ValueError("Authentication configuration is incomplete.")
            try:
                key = self.auth_verifier_key.get_secret_value()
                if len(key) != 44 or len(base64.b64decode(key, altchars=b"-_", validate=True)) != 32:
                    raise ValueError
            except (ValueError, UnicodeError):
                raise ValueError("Authentication key is invalid.") from None
        return self

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

    @field_validator("port", "max_request_body_bytes", "request_body_timeout_seconds", "database_pool_max_size", mode="before")
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
