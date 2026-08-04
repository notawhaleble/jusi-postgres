from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


PLUGIN_OPTIONS = {
    "provider",
    "initial_fetch",
    "krb5ccname",
    "collect_metadata",
    "skip_metadata",
    "metadata_max_rows",
    "metadata_schemas",
}

DEFAULT_METADATA_MAX_ROWS = 1_000_000


@dataclass(frozen=True)
class PostgresOptions:
    connect: dict[str, Any]
    initial_fetch: int
    krb5ccname: str
    collect_metadata: bool
    metadata_max_rows: int
    metadata_schemas: tuple[str, ...]


def parse_postgres_options(options: Mapping[str, Any]) -> PostgresOptions:
    raw_initial = options.get("initial_fetch", 100)
    try:
        initial_fetch = int(raw_initial)
    except (TypeError, ValueError):
        initial_fetch = 100
    if initial_fetch < 0:
        initial_fetch = 0
    krb5ccname = str(options.get("krb5ccname", "")).strip()
    collect_metadata = _parse_bool(options.get("collect_metadata", True), default=True)
    if _parse_bool(options.get("skip_metadata", False), default=False):
        collect_metadata = False
    raw_metadata_max_rows = options.get("metadata_max_rows", DEFAULT_METADATA_MAX_ROWS)
    try:
        metadata_max_rows = int(raw_metadata_max_rows)
    except (TypeError, ValueError):
        metadata_max_rows = DEFAULT_METADATA_MAX_ROWS
    if metadata_max_rows < 1:
        metadata_max_rows = DEFAULT_METADATA_MAX_ROWS
    metadata_schemas = _parse_metadata_schemas(options.get("metadata_schemas", ()))
    connect = {str(key): value for key, value in options.items() if str(key) not in PLUGIN_OPTIONS}
    return PostgresOptions(
        connect=connect,
        initial_fetch=initial_fetch,
        krb5ccname=krb5ccname,
        collect_metadata=collect_metadata,
        metadata_max_rows=metadata_max_rows,
        metadata_schemas=metadata_schemas,
    )


def _parse_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_metadata_schemas(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    schemas: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        schema = str(item).strip()
        if not schema or schema in seen:
            continue
        seen.add(schema)
        schemas.append(schema)
    return tuple(schemas)


@contextmanager
def kerberos_cache_env(path: str) -> Iterator[None]:
    if not path:
        yield
        return
    previous = os.environ.get("KRB5CCNAME")
    os.environ["KRB5CCNAME"] = path
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("KRB5CCNAME", None)
        else:
            os.environ["KRB5CCNAME"] = previous
