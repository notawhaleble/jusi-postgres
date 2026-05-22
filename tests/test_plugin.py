from __future__ import annotations

import os
import threading

from jusi.plugins import DisplayHandlerSpec

from jusi_postgres.config import kerberos_cache_env, parse_postgres_options
from jusi_postgres.completion import completion_items, parse_query_relations
from jusi.domain.models import JUSI_HANDLER_HANDOFF_MIME

from jusi_postgres.constants import POSTGRES_BOOTSTRAP_SQL
from jusi_postgres.kernel import _parse_sql_line, _sql_blank_body_transformer, configure_sql_session, register_sql_magic
from jusi_postgres.metadata import CompletionColumn, CompletionObject, MetadataCache, MetadataSnapshot
from jusi_postgres.plugin import PostgresHandler, display_handler_specs
from jusi_postgres.runner import _looks_like_result_query, _write_raw_value_file
from jusi_postgres.state import target_cache_dir


def test_display_handler_spec_registers_sql_magic() -> None:
    specs = display_handler_specs()
    assert len(specs) == 1
    spec = specs[0]
    assert isinstance(spec, DisplayHandlerSpec)
    assert spec.handler_id == "postgres"
    assert spec.magic_commands[0].name == "sql"
    assert spec.kernel_extension_modules == ("jusi_postgres.kernel",)
    assert spec.presentation["completion"] is True


def test_bootstrap_body_is_safe_empty_result_query() -> None:
    assert PostgresHandler.bootstrap_cell_body("analytics") == "SELECT 1 AS jusi_bootstrap WHERE false"


def test_show_and_explain_do_not_use_named_cursor_path() -> None:
    assert _looks_like_result_query("select 1") is True
    assert _looks_like_result_query("show max_connections") is False
    assert _looks_like_result_query("explain select 1") is False


def test_parse_postgres_options_keeps_psycopg_options_and_plugin_options() -> None:
    options = parse_postgres_options(
        {
            "provider": "postgres",
            "host": "db.example",
            "dbname": "analytics",
            "password": "secret",
            "initial_fetch": "25",
            "krb5ccname": "/tmp/krb5cc_test",
        }
    )
    assert options.initial_fetch == 25
    assert options.krb5ccname == "/tmp/krb5cc_test"
    assert options.connect == {"host": "db.example", "dbname": "analytics", "password": "secret"}


def test_parse_sql_line_supports_initial_fetch_magic_arg() -> None:
    alias, options = _parse_sql_line("analytics --initial-fetch 7")
    assert alias == "analytics"
    assert options == {"initial_fetch": 7}


def test_kernel_substitutes_postgres_blank_body_bootstrap(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: list[dict[str, object]] = []

    class FakeMagicsManager:
        magics = {"cell": {}}

    class FakeIp:
        magics_manager = FakeMagicsManager()

        def register_magic_function(self, func, *, magic_kind: str, magic_name: str) -> None:  # type: ignore[no-untyped-def]
            assert magic_kind == "cell"
            assert magic_name == "sql"
            self.magic = func

    def fake_display(payload, *, raw=False, metadata=None):  # type: ignore[no-untyped-def]
        captured.append({"payload": payload, "raw": raw, "metadata": metadata})

    monkeypatch.setattr("IPython.display.display", fake_display)
    configure_sql_session({"sql": {"local_postgres": {"provider": "postgres", "host": "127.0.0.1"}}})
    fake_ip = FakeIp()
    register_sql_magic(fake_ip)

    fake_ip.magic("local_postgres", "\n")

    handoff = captured[0]["payload"][JUSI_HANDLER_HANDOFF_MIME]  # type: ignore[index]
    assert handoff["content"] == POSTGRES_BOOTSTRAP_SQL


def test_kernel_transformer_adds_body_for_header_only_postgres_magic() -> None:
    configure_sql_session({"sql": {"local_postgres": {"provider": "postgres", "host": "127.0.0.1"}}})
    assert _sql_blank_body_transformer(["%%sql local_postgres\n"]) == [
        "%%sql local_postgres\n",
        POSTGRES_BOOTSTRAP_SQL + "\n",
    ]


def test_kerberos_cache_env_restores_previous_value(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("KRB5CCNAME", "old")
    with kerberos_cache_env("new"):
        assert os.environ["KRB5CCNAME"] == "new"
    assert os.environ["KRB5CCNAME"] == "old"


def test_target_cache_dir_redacts_secret_values(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("JUSI_STATE_HOME", str(tmp_path))
    first = target_cache_dir("analytics", {"host": "db", "password": "one"})
    second = target_cache_dir("analytics", {"host": "db", "password": "two"})
    assert first == second
    assert str(first).startswith(str(tmp_path / "plugins" / "postgres" / "analytics"))


def test_parse_query_relations_extracts_aliases() -> None:
    relations = parse_query_relations(
        "select u.id, o.total from public.users u join orders as o on o.user_id = u.id"
    )
    assert ("public", "users", "u") in [(item.schema, item.table, item.alias) for item in relations]
    assert ("", "orders", "o") in [(item.schema, item.table, item.alias) for item in relations]


def test_completion_items_include_alias_columns() -> None:
    snapshot = MetadataSnapshot(
        schemas=["public"],
        objects=[CompletionObject(schema="public", name="users", kind="table")],
        columns=[CompletionColumn(schema="public", table="users", name="email", data_type="text")],
        functions=[CompletionObject(schema="public", name="lower", kind="function", detail="text")],
        refreshed_at=1.0,
    )
    items = completion_items(
        snapshot,
        {
            "current_word": "e",
            "cursor_col": len("select u.e"),
            "line_text": "select u.e",
            "cell_text": "%%sql local_postgres\nselect u.e from public.users u",
        },
    )
    alias_item = next(item for item in items if item["value"] == "u.email" and item["kind"] == "column")
    assert alias_item["start_col"] == len("select ")
    assert alias_item["end_col"] == len("select u.e")


def test_completion_span_handles_vim_cursor_col_after_word() -> None:
    snapshot = MetadataSnapshot(
        schemas=["public"],
        objects=[CompletionObject(schema="public", name="items", kind="table")],
        columns=[CompletionColumn(schema="public", table="items", name="value", data_type="text")],
        functions=[],
        refreshed_at=1.0,
    )
    items = completion_items(
        snapshot,
        {
            "current_word": "value",
            "cursor_col": 13,
            "line_text": "select value from items",
            "cell_text": "%%sql local_postgres\nselect value from items",
        },
    )
    value_item = next(item for item in items if item["value"] == "value" and item["kind"] == "column")
    assert value_item["start_col"] == len("select ")
    assert value_item["end_col"] == len("select value")


def test_write_raw_value_file_preserves_binary_bytes() -> None:
    path = _write_raw_value_file(b"PK\x05\x06" + (b"\x00" * 18), ".zip")
    with open(path, "rb") as handle:
        assert handle.read(4) == b"PK\x05\x06"


def test_metadata_snapshot_does_not_refresh_until_cell_entry(tmp_path) -> None:  # type: ignore[no-untyped-def]
    calls: list[bool] = []

    def load() -> MetadataSnapshot:
        calls.append(True)
        return MetadataSnapshot(schemas=["public"])

    cache = MetadataCache(tmp_path, load)
    assert cache.snapshot().schemas == []
    assert calls == []
    assert cache.ensure_fresh_async() is True
    assert cache.close(timeout=2.0) is True
    assert calls == [True]
    assert cache.snapshot().schemas == ["public"]


def test_metadata_refresh_deduplicates_concurrent_cell_entries(tmp_path) -> None:  # type: ignore[no-untyped-def]
    started = threading.Event()
    release = threading.Event()
    calls: list[bool] = []

    def load() -> MetadataSnapshot:
        calls.append(True)
        started.set()
        release.wait(timeout=2.0)
        return MetadataSnapshot(schemas=["public"])

    cache = MetadataCache(tmp_path, load)
    assert cache.ensure_fresh_async() is True
    assert started.wait(timeout=2.0)
    assert cache.ensure_fresh_async() is False
    release.set()
    assert cache.close(timeout=2.0) is True
    assert calls == [True]
