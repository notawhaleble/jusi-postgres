"""Thin exact-provider adapter for the shared Jusi SQL kernel dispatcher."""
from __future__ import annotations

from jusi_sql.kernel import SqlKernelAdapter

from . import __version__
from .constants import POSTGRES_BOOTSTRAP_SQL


_adapter = SqlKernelAdapter(
    plugin_id="postgres",
    plugin_version=__version__,
    selectors=("postgres", "postgresql"),
    empty_sql=POSTGRES_BOOTSTRAP_SQL,
)

jusi_kernel_adapter_v1 = _adapter.manifest
configure_jusi_runtime_v1 = _adapter.configure
load_ipython_extension = _adapter.load
