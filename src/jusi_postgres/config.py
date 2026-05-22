from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


PLUGIN_OPTIONS = {"provider", "initial_fetch", "krb5ccname"}


@dataclass(frozen=True)
class PostgresOptions:
    connect: dict[str, Any]
    initial_fetch: int
    krb5ccname: str


def parse_postgres_options(options: Mapping[str, Any]) -> PostgresOptions:
    raw_initial = options.get("initial_fetch", 100)
    try:
        initial_fetch = int(raw_initial)
    except (TypeError, ValueError):
        initial_fetch = 100
    if initial_fetch < 0:
        initial_fetch = 0
    krb5ccname = str(options.get("krb5ccname", "")).strip()
    connect = {str(key): value for key, value in options.items() if str(key) not in PLUGIN_OPTIONS}
    return PostgresOptions(connect=connect, initial_fetch=initial_fetch, krb5ccname=krb5ccname)


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
