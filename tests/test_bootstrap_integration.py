"""Real kernel coexistence; no database server or credentials are required."""
import importlib.util

import pytest

from jusi.application.ports import KernelAdapterSpec
from jusi.infrastructure.jupyter_kernel import ManagedJupyterKernelFactory


def test_real_kernel_bootstraps_both_sql_providers_without_queries():
    if importlib.util.find_spec('jusi_clickhouse') is None:
        pytest.skip('Sibling ClickHouse provider needed for coexistence integration')
    from jusi_postgres import __version__ as pg_version
    from jusi_clickhouse import __version__ as ch_version
    from jusi_postgres.catalog import catalog_entry as postgres_catalog
    from jusi_clickhouse.catalog import catalog_entry as clickhouse_catalog
    from jusi.protocol import validate_plugin_catalog

    validate_plugin_catalog({'protocol_version': 1, 'catalog_version': 1,
                             'discovery_id': 'bootstrap_coexistence',
                             'plugins': [postgres_catalog(), clickhouse_catalog()]})

    kernel = ManagedJupyterKernelFactory().start('python3', timeout=10, adapters=(
        KernelAdapterSpec('postgres', pg_version, 'jusi_postgres.kernel', (('sql', 'sql'),)),
        KernelAdapterSpec('clickhouse', ch_version, 'jusi_clickhouse.kernel', (('sql', 'sql'),)),
    ), configuration={'sql': {'targets': {
        'pg': {'provider': 'postgres', 'host': 'unused-pg'},
        'ch': {'provider': 'clickhouse', 'host': 'unused-ch'},
    }}})
    try:
        for alias, provider in [('pg', 'postgres'), ('ch', 'clickhouse')]:
            result = kernel.bootstrap('sql', alias, timeout=5, on_output=lambda _: None)
            assert result.outcome == 'succeeded'
            assert len(result.handoffs) == 1
            handoff = result.handoffs[0]
            assert handoff.plugin_id == provider
            assert handoff.payload['bootstrap'] is True
            assert handoff.payload['sql'] == ''
        assert kernel.bootstrap('sql', 'missing', timeout=5, on_output=lambda _: None).outcome == 'failed'
        assert kernel.execute('1+1', timeout=5, on_output=lambda _: None).outcome == 'succeeded'
    finally:
        kernel.stop(timeout=5)
