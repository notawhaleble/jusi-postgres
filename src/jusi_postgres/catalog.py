"""Lightweight Jusi 1.0 catalog provider; imports no runtime dependencies."""
from __future__ import annotations

from typing import Any

from jusi_sql import sql_catalog_entry

from . import __version__


def catalog_entry() -> dict[str, Any]:
    return sql_catalog_entry(
        plugin_id="postgres",
        plugin_version=__version__,
        distribution="jusi-postgres",
        kernel_extension="jusi_postgres.kernel",
        worker_entry_point="jusi_postgres.worker:create_worker",
        provider_presentation={"syntax": "pgsql", "indent": "sql"},
    )
