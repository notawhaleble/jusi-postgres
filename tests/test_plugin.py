from __future__ import annotations

import os
import threading
from types import SimpleNamespace

from jusi.plugins import DisplayHandlerSpec

from jusi_postgres.config import kerberos_cache_env, parse_postgres_options
from jusi_postgres.completion import completion_items, parse_query_relations
from jusi.domain.models import JUSI_HANDLER_HANDOFF_MIME

from jusi_postgres.constants import POSTGRES_BOOTSTRAP_SQL
from jusi_postgres.kernel import _parse_sql_line, _sql_blank_body_transformer, configure_sql_session, register_sql_magic
from jusi_postgres.metadata import CompletionColumn, CompletionObject, MetadataCache, MetadataSnapshot
from jusi_postgres.plugin import PostgresHandler, display_handler_specs
from jusi_postgres.runner import PostgresResultSheet, RawBinaryValue, _display_row, _looks_like_result_query, _write_raw_value_file
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


def test_completion_items_walk_database_objects_one_level_at_a_time() -> None:
    snapshot = MetadataSnapshot(
        schemas=["demo", "public"],
        objects=[
            CompletionObject(schema="demo", name="accounts", kind="table"),
            CompletionObject(schema="demo", name="events", kind="table"),
            CompletionObject(schema="public", name="users", kind="table"),
        ],
        columns=[
            CompletionColumn(schema="demo", table="accounts", name="email", data_type="text"),
            CompletionColumn(schema="demo", table="events", name="payload", data_type="jsonb"),
        ],
        functions=[CompletionObject(schema="demo", name="decode_event", kind="function", detail="jsonb")],
        refreshed_at=1.0,
    )

    top_level = completion_items(
        snapshot,
        {
            "current_word": "d",
            "cursor_col": len("select d"),
            "line_text": "select d",
            "cell_text": "%%sql local_postgres\nselect d",
        },
    )
    assert {item["value"] for item in top_level if item["kind"] == "schema"} == {"demo"}
    assert all(item["kind"] != "keyword" for item in top_level)
    assert "SELECT" not in {item["value"] for item in top_level}
    assert "demo.accounts" not in {item["value"] for item in top_level}
    assert "demo.accounts.email" not in {item["value"] for item in top_level}

    empty_word = completion_items(
        snapshot,
        {
            "current_word": "",
            "cursor_col": len("select "),
            "line_text": "select ",
            "cell_text": "%%sql local_postgres\nselect ",
        },
    )
    assert {item["value"] for item in empty_word if item["kind"] == "schema"} == {"demo", "public"}
    assert {item["start_col"] for item in empty_word} == {len("select ")}
    assert {item["end_col"] for item in empty_word} == {len("select ")}

    schema_members = completion_items(
        snapshot,
        {
            "current_word": "a",
            "cursor_col": len("select demo.a"),
            "line_text": "select demo.a",
            "cell_text": "%%sql local_postgres\nselect demo.a",
        },
    )
    assert [item["value"] for item in schema_members] == ["demo.accounts"]
    assert all(item["kind"] != "column" for item in schema_members)

    table_columns = completion_items(
        snapshot,
        {
            "current_word": "e",
            "cursor_col": len("select demo.accounts.e"),
            "line_text": "select demo.accounts.e",
            "cell_text": "%%sql local_postgres\nselect demo.accounts.e",
        },
    )
    assert [item["value"] for item in table_columns] == ["demo.accounts.email"]


def test_completion_span_handles_vim_cursor_col_after_qualified_word() -> None:
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
            "cursor_col": len("select items.value"),
            "line_text": "select items.value from items",
            "cell_text": "%%sql local_postgres\nselect items.value from items",
        },
    )
    value_item = next(item for item in items if item["value"] == "items.value" and item["kind"] == "column")
    assert value_item["start_col"] == len("select ")
    assert value_item["end_col"] == len("select items.value")


def test_write_raw_value_file_preserves_binary_bytes() -> None:
    path = _write_raw_value_file(b"PK\x05\x06" + (b"\x00" * 18), ".zip")
    with open(path, "rb") as handle:
        assert handle.read(4) == b"PK\x05\x06"


def test_binary_values_render_as_placeholder_but_preserve_raw_bytes() -> None:
    row = _display_row([1, memoryview(b"abc"), bytearray(b"defg")])

    assert row[0] == 1
    assert isinstance(row[1], RawBinaryValue)
    assert str(row[1]) == "<binary data: 3 bytes>"
    assert repr(row[2]) == "<binary data: 4 bytes>"

    path = _write_raw_value_file(row[1], ".bin")
    with open(path, "rb") as handle:
        assert handle.read() == b"abc"


def test_fetch_more_does_not_change_plugin_execution_status(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    emitted_statuses: list[str] = []
    added_rows: list[list[object]] = []
    notified: list[bool] = []

    sheet = object.__new__(PostgresResultSheet)
    sheet.session = SimpleNamespace(lock=threading.RLock())
    sheet.cursor_closed_reason = ""
    sheet.exhausted = False
    sheet.cursor = object()

    monkeypatch.setattr("jusi_postgres.runner.set_plugin_execution_status", lambda status: emitted_statuses.append(status))
    monkeypatch.setattr(PostgresResultSheet, "_fetch_rows", lambda self, count: [("new", b"raw")])
    monkeypatch.setattr(PostgresResultSheet, "addRow", lambda self, row: added_rows.append(row))
    monkeypatch.setattr(PostgresResultSheet, "_notify_more", lambda self: notified.append(True))

    assert sheet.fetch_more(5) == 1
    assert added_rows[0][0] == "new"
    assert isinstance(added_rows[0][1], RawBinaryValue)
    assert str(added_rows[0][1]) == "<binary data: 3 bytes>"
    assert notified == [True]
    assert emitted_statuses == []


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
