"""Service-only environment configuration; secrets never enter diagnostics."""
from dataclasses import dataclass, field
import os
import re
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    def __init__(self):
        super().__init__("Invalid reimbursement service configuration.")


def valid_schema(value: str) -> bool:
    return value == "reimbursement" or bool(re.fullmatch(r"ref01_probe_[a-f0-9]{32}", value))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default="", repr=False)
    schema: str = "reimbursement"
    host: str = "127.0.0.1"
    port: int = 8010
    pool_size: int = 2
    catalog_enabled: bool = False
    claim_access_enabled: bool = False
    draft_sources_enabled: bool = False
    action_sources_enabled: bool = False
    workflow_enabled: bool = False
    recovery_credential: str = field(default='',repr=False)
    terminal_key_id: str = ''
    terminal_private_hex: str = field(default='',repr=False)
    platform_origin: str = ''
    platform_credential: str = field(default='',repr=False)
    dispatcher_credential: str = field(default='',repr=False)

    def __post_init__(self):
        if (not isinstance(self.database_url, str) or not self.database_url
                or not valid_schema(self.schema) or self.host not in {"127.0.0.1", "0.0.0.0"}
                or type(self.port) is not int or not 1 <= self.port <= 65535
                or type(self.pool_size) is not int or not 1 <= self.pool_size <= 2):
            raise ConfigurationError()
        if type(self.catalog_enabled) is not bool:raise ConfigurationError()
        if type(self.claim_access_enabled) is not bool or (self.claim_access_enabled and not self.catalog_enabled):raise ConfigurationError()
        if type(self.draft_sources_enabled) is not bool or (self.draft_sources_enabled and not self.claim_access_enabled):raise ConfigurationError()
        if type(self.action_sources_enabled) is not bool or (self.action_sources_enabled and not self.claim_access_enabled):raise ConfigurationError()
        if type(self.workflow_enabled) is not bool:raise ConfigurationError()
        if self.workflow_enabled and (not self.action_sources_enabled or any(type(v) is not str for v in (self.recovery_credential,self.terminal_key_id,self.terminal_private_hex)) or not re.fullmatch('[A-Za-z0-9_-]{43}',self.recovery_credential)
          or self.recovery_credential in {self.platform_credential,self.dispatcher_credential} or not re.fullmatch('[A-Za-z0-9_-]{1,64}',self.terminal_key_id)
          or not re.fullmatch('[a-f0-9]{64}',self.terminal_private_hex)):raise ConfigurationError()
        if self.catalog_enabled:
            try:
                target=urlsplit(self.platform_origin)
                if re.search(r'[\x00-\x20\x7f]',self.platform_origin):raise ValueError
                if target.scheme!='https' or not target.hostname or target.username or target.password or target.path or target.query or target.fragment:raise ValueError
                if target.port is not None and not 1<=target.port<=65535:raise ValueError
                if any(not re.fullmatch('[A-Za-z0-9_-]{43}',v) for v in (self.platform_credential,self.dispatcher_credential)):raise ValueError
                if self.platform_credential==self.dispatcher_credential:raise ValueError
            except (ValueError,TypeError):raise ConfigurationError() from None


def load_settings(environ=None):
    env = os.environ if environ is None else environ
    try:
        return Settings(
            database_url=env.get("REIMBURSEMENT_DATABASE_URL", ""),
            schema=env.get("REIMBURSEMENT_SCHEMA", "reimbursement"),
            host=env.get("REIMBURSEMENT_HOST", "127.0.0.1"),
            port=int(env.get("REIMBURSEMENT_PORT", "8010")),
            pool_size=int(env.get("REIMBURSEMENT_POOL_SIZE", "2")),
            catalog_enabled={'true':True,'false':False}[env.get('REIMBURSEMENT_CATALOG_ENABLED','false')],
            claim_access_enabled={'true':True,'false':False}[env.get('REIMBURSEMENT_CLAIM_ACCESS_ENABLED','false')],
            draft_sources_enabled={'true':True,'false':False}[env.get('REIMBURSEMENT_DRAFT_SOURCES_ENABLED','false')],
            action_sources_enabled={'true':True,'false':False}[env.get('REIMBURSEMENT_ACTION_SOURCES_ENABLED','false')],
            workflow_enabled={'true':True,'false':False}[env.get('REIMBURSEMENT_WORKFLOW_ENABLED','false')],
            recovery_credential=env.get('REIMBURSEMENT_RECOVERY_CREDENTIAL',''),terminal_key_id=env.get('REIMBURSEMENT_TERMINAL_KEY_ID',''),terminal_private_hex=env.get('REIMBURSEMENT_TERMINAL_PRIVATE_HEX',''),
            platform_origin=env.get('REIMBURSEMENT_PLATFORM_ORIGIN',''),
            platform_credential=env.get('REIMBURSEMENT_PLATFORM_CREDENTIAL',''),
            dispatcher_credential=env.get('REIMBURSEMENT_DISPATCHER_CREDENTIAL',''),
        )
    except (ValueError, TypeError,KeyError):
        raise ConfigurationError() from None
