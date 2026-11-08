"""Strict TLS-only targets and sanitized driver logging."""
from dataclasses import dataclass
import logging
import re
from urllib.parse import parse_qsl, unquote, urlsplit
import certifi


class TargetError(ValueError):
    def __init__(self):
        super().__init__("Invalid service database target.")


class SafeDriverLog(logging.Filter):
    def filter(self, record):
        record.msg, record.args = "Service database driver event.", ()
        record.exc_info = record.exc_text = record.stack_info = None
        return True


def safe_driver_logging():
    for name in ("psycopg", "psycopg.pool", "psycopg.transaction", "psycopg.waiting",
                 "psycopg_pool.sched", "psycopg_pool.sched_async"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, SafeDriverLog) for item in logger.filters):
            logger.addFilter(SafeDriverLog())


@dataclass(frozen=True, repr=False)
class Target:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    channel_binding: str = "prefer"

    def __repr__(self):
        return "Target(<redacted>)"

    def kwargs(self):
        return dict(host=self.host, port=self.port, dbname=self.dbname, user=self.user,
                    password=self.password, sslmode="verify-full", sslrootcert=certifi.where(),
                    channel_binding=self.channel_binding, gssencmode="disable",
                    connect_timeout=5, autocommit=True, prepare_threshold=None)


def parse_target(value: str, *, direct=False) -> Target:
    try:
        if not value or re.search(r"[\x00-\x20\x7f]", value) or re.search(r"%(?![a-fA-F0-9]{2})", value):
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme not in {"postgres", "postgresql"} or not parts.hostname
                or not re.fullmatch(r"[A-Za-z0-9.-]+", parts.hostname) or parts.fragment
                or not parts.username or not parts.password or not parts.path.startswith("/")
                or (direct and "-pooler" in parts.hostname)):
            raise ValueError
        values = [unquote(item, errors="strict") for item in (parts.username, parts.password, parts.path[1:])]
        if any(not item or re.search(r"[\x00-\x1f\x7f]", item) for item in values):
            raise ValueError
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        options = dict(pairs)
        if len(options) != len(pairs) or set(options) - {"sslmode", "channel_binding"}:
            raise ValueError
        if options.get("sslmode", "verify-full") not in {"require", "verify-full"}:
            raise ValueError
        binding = options.get("channel_binding", "prefer")
        port = 5432 if parts.port is None else parts.port
        if binding not in {"prefer", "require"} or not 1 <= port <= 65535:
            raise ValueError
        return Target(parts.hostname.lower(), port, values[2], values[0], values[1], binding)
    except (ValueError, UnicodeError, TypeError):
        raise TargetError() from None


def schema_and_role(schema: str) -> str:
    if schema == "reimbursement":
        return "reimbursement_runtime"
    if re.fullmatch(r"ref01_probe_[a-f0-9]{32}", schema):
        return "ref01_runtime_" + schema.removeprefix("ref01_probe_")
    raise TargetError()


def check_pair(migration: Target, runtime: Target, schema: str):
    if (runtime.user != schema_and_role(schema) or migration.user == runtime.user
            or (runtime.host.replace("-pooler.", "."), runtime.port, runtime.dbname)
            != (migration.host, migration.port, migration.dbname)):
        raise TargetError()
