from __future__ import annotations

import importlib
import json
import socket
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from jusi.plugin_api import OperationRejected, WorkerContext
from jusi.protocol import validate_plugin_kernel_message
from jusi_sql import CompletionColumn, CompletionObject, MetadataSnapshot, SqlCompletionRequest, find_sql_actions
from jusi_sql.kernel import _reset_runtime_for_tests, dispatch_sql

from jusi_postgres.catalog import catalog_entry
from jusi_postgres.config import kerberos_cache_env, parse_postgres_options
from jusi_postgres.constants import POSTGRES_BOOTSTRAP_SQL
from jusi_postgres.ipc import ApplicationController, WorkerApplicationBridge
from jusi_postgres.metadata import MetadataLimitExceeded, load_postgres_metadata
from jusi_postgres.runner import (
    PostgresResultSheet,
    PostgresSession,
    RawBinaryValue,
    _complete_postgres_sql,
    _connect_psycopg,
    _display_row,
    _followup_sql,
    _initialize_visidata_application,
    _looks_like_result_query,
    _write_raw_value_file,
)
from jusi_postgres.worker import PostgresClientSession, create_worker


def worker_context() -> WorkerContext:
    return WorkerContext("worker_1", "runtime_1", "postgres", "sql", "client_1", "execution_1")


def test_catalog_is_an_exact_jusi_1_sql_provider() -> None:
    entry = catalog_entry()
    assert entry == {
        "plugin_id": "postgres",
        "plugin_version": "0.2.1",
        "distribution": "jusi-postgres",
        "families": [{
            "family_id": "sql",
            "magic_name": "sql",
            "capabilities": ["execute", "followup", "complete", "interrupt", "editor_actions"],
            "presentation": {"syntax": "sql", "indent": "sql"},
            "provider_presentation": {"syntax": "pgsql", "indent": "sql"},
        }],
        "kernel_extensions": ["jusi_postgres.kernel"],
        "worker_entry_point": "jusi_postgres.worker:create_worker",
        "media_types": ["text/x-ansi"],
        "interaction": "terminal_interactive",
    }


def test_catalog_import_does_not_load_runtime_dependencies(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for name in list(sys.modules):
        if name == "jusi_postgres.catalog" or name.startswith(("psycopg", "visidata", "IPython")):
            monkeypatch.delitem(sys.modules, name, raising=False)
    importlib.import_module("jusi_postgres.catalog").catalog_entry()
    assert not any(name.startswith(("psycopg", "visidata", "IPython")) for name in sys.modules)


class FakeIp:
    def __init__(self) -> None:
        self.magics_manager = SimpleNamespace(magics={"cell": {}})
        self.registrations = []

    def register_magic_function(self, function, *, magic_kind, magic_name):  # type: ignore[no-untyped-def]
        self.magics_manager.magics[magic_kind][magic_name] = function
        self.registrations.append((magic_kind, magic_name))


def test_kernel_uses_family_dispatcher_and_exact_handoff() -> None:
    _reset_runtime_for_tests()
    kernel = importlib.reload(importlib.import_module("jusi_postgres.kernel"))
    kernel.configure_jusi_runtime_v1({
        "sql": {"targets": {"analytics": {"provider": "postgresql", "host": "db"}}}
    })
    ipython = FakeIp()
    kernel.load_ipython_extension(ipython)
    assert ipython.registrations == [("cell", "sql")]
    handoff = dispatch_sql("analytics", "")
    validate_plugin_kernel_message(handoff)
    assert handoff["plugin_id"] == "postgres"
    assert handoff["payload"] == {
        "alias": "analytics", "sql": POSTGRES_BOOTSTRAP_SQL, "options": {"host": "db"},
    }
    _reset_runtime_for_tests()


def test_parse_postgres_options_keeps_only_driver_options() -> None:
    options = parse_postgres_options({
        "host": "db.example", "dbname": "analytics", "password": "secret",
        "initial_fetch": "25", "krb5ccname": "/tmp/krb5cc_test",
        "collect_metadata": "false", "metadata_max_rows": "250",
        "metadata_schemas": "public,demo",
    })
    assert options.initial_fetch == 25
    assert options.krb5ccname == "/tmp/krb5cc_test"
    assert options.collect_metadata is False
    assert options.metadata_max_rows == 250
    assert options.metadata_schemas == ("public", "demo")
    assert options.connect == {"host": "db.example", "dbname": "analytics", "password": "secret"}


def test_kerberos_cache_env_restores_previous_value(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("KRB5CCNAME", "old")
    with kerberos_cache_env("new"):
        assert __import__("os").environ["KRB5CCNAME"] == "new"
    assert __import__("os").environ["KRB5CCNAME"] == "old"


def test_followup_preserves_literal_sql_but_removes_magic_header() -> None:
    assert _followup_sql("select 1") == "select 1"
    assert _followup_sql("%%sql analytics\nselect α") == "select α"


def test_blank_relation_completion_returns_only_schemas() -> None:
    prefix = "select * from "
    request = SqlCompletionRequest(prefix, prefix, len(prefix), 0, len(prefix))
    snapshot = MetadataSnapshot(
        schemas=["demo", "public"],
        objects=[CompletionObject("accounts", "demo", "table")],
        columns=[CompletionColumn("accounts", "email", "demo", "text")],
        functions=[CompletionObject("decode_event", "demo", "function")],
    )

    result = _complete_postgres_sql(snapshot, request)

    assert [(item["text"], item["kind"]) for item in result["items"]] == [
        ("demo", "schema"),
        ("public", "schema"),
    ]
    assert {(item["start"], item["end"]) for item in result["items"]} == {
        (len(prefix), len(prefix)),
    }


def test_typed_relation_completion_keeps_matching_metadata() -> None:
    prefix = "select * from acc"
    request = SqlCompletionRequest(prefix, prefix, len(prefix), 0, len(prefix))
    snapshot = MetadataSnapshot(
        schemas=["demo"],
        objects=[CompletionObject("accounts", "demo", "table")],
    )

    result = _complete_postgres_sql(snapshot, request)

    assert "accounts" in {item["text"] for item in result["items"]}


def test_application_loads_visidata_config_before_provider_commands(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []
    monkeypatch.setattr(
        "jusi.visidata_support.initialize_visidata",
        lambda **kwargs: calls.append(("initialize", kwargs)),
    )
    monkeypatch.setattr(
        "jusi_postgres.runner.install_postgres_commands",
        lambda: calls.append(("commands", {})),
    )

    _initialize_visidata_application()

    assert calls == [
        ("initialize", {"open_name": "selection.sql", "open_filetype": "sql"}),
        ("commands", {}),
    ]


def test_worker_returns_one_terminal_and_delegates_to_family_router(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("jusi_postgres.worker.find_spec", lambda _name: object())
    worker = create_worker(worker_context())
    result = worker.handle("execute", {"alias": "analytics", "sql": "select 1", "options": {"host": "db"}})
    assert result.result == {"accepted": True, "alias": "analytics"}
    assert len(result.core_requests) == 1
    surface = result.core_requests[0]
    assert surface.request_id == "postgres_visidata"
    assert surface.argv[1:3] == ("-m", "jusi_postgres.runner")
    assert "signal" in surface.capabilities
    with pytest.raises(OperationRejected, match="already started"):
        worker.handle("execute", {"alias": "analytics", "sql": "select 2", "options": {}})
    worker.close()


def test_worker_editor_actions_are_content_based() -> None:
    session = PostgresClientSession(worker_context())
    assert session.editor_action("copy", {"text": "select α", "linewise": True}).result == {
        "action": "copy", "text": "select α", "regtype": "V",
    }
    assert session.editor_action("open", {"text": "select 1"}).result["name"] == "selection.sql"
    with pytest.raises(OperationRejected, match="requires text"):
        session.editor_action("copy", {})


def test_private_bridge_round_trip_and_interrupt(tmp_path: Path) -> None:
    socket_path = f"/tmp/jusi-pg-test-{__import__('os').getpid()}.sock"
    Path(socket_path).unlink(missing_ok=True)
    bridge = WorkerApplicationBridge(socket_path)
    interrupted = threading.Event()

    def handle(operation: str, payload: dict) -> dict:
        if operation == "interrupt":
            interrupted.set()
        return {"operation": operation, **payload}

    controller = ApplicationController(bridge.socket_path, handle)
    controller.start()
    assert bridge.request("followup", {"body": "select 2"})["result"]["body"] == "select 2"
    bridge.interrupt()
    assert interrupted.wait(1)
    bridge.close()


def test_result_sheet_uses_family_actions() -> None:
    session = SimpleNamespace(alias="analytics", register_sheet=lambda _sheet: None, commit=lambda: None, rollback=lambda: None)
    sheet = PostgresResultSheet(session=session, query="select 1")
    actions = find_sql_actions(sheet)
    assert actions is not None
    assert actions.fetch_more.__self__ is sheet


def test_show_and_explain_keep_legacy_cursor_behavior() -> None:
    assert _looks_like_result_query("select 1") is True
    assert _looks_like_result_query("show max_connections") is False
    assert _looks_like_result_query("explain select 1") is False


def test_binary_values_render_as_placeholder_and_preserve_bytes(tmp_path: Path) -> None:
    row = _display_row([1, memoryview(b"abc"), bytearray(b"defg")])
    assert isinstance(row[1], RawBinaryValue)
    assert str(row[1]) == "<binary data: 3 bytes>"
    path = _write_raw_value_file(row[1], ".bin")
    assert Path(path).read_bytes() == b"abc"
    Path(path).unlink()


def test_connect_psycopg_registers_infinity_loaders(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class Adapters:
        def __init__(self) -> None:
            self.loaders = {}

        def register_loader(self, name, loader):  # type: ignore[no-untyped-def]
            self.loaders[name] = loader

    connection = SimpleNamespace(adapters=Adapters())

    class Loader:
        def load(self, data):  # type: ignore[no-untyped-def]
            return ("decoded", bytes(data))

    psycopg = ModuleType("psycopg")
    psycopg.connect = lambda **_options: connection  # type: ignore[attr-defined]
    types = ModuleType("psycopg.types")
    datetime = ModuleType("psycopg.types.datetime")
    datetime.DateLoader = datetime.TimestampLoader = datetime.TimestamptzLoader = Loader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.types", types)
    monkeypatch.setitem(sys.modules, "psycopg.types.datetime", datetime)
    assert _connect_psycopg({}) is connection
    assert connection.adapters.loaders["timestamptz"]().load(b"infinity") == "infinity"
    assert connection.adapters.loaders["date"]().load(b"2026-08-05") == ("decoded", b"2026-08-05")


class FakeMetadataCursor:
    def __init__(self, result_sets):  # type: ignore[no-untyped-def]
        self.result_sets = result_sets
        self.executed = []
        self.params = []

    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def execute(self, query, params=()):  # type: ignore[no-untyped-def]
        self.executed.append(query); self.params.append(params); self.rows = self.result_sets.pop(0)
    def fetchmany(self, count): return self.rows[:count]  # type: ignore[no-untyped-def]


class FakeMetadataConnection:
    def __init__(self, result_sets):  # type: ignore[no-untyped-def]
        self.cursor_obj = FakeMetadataCursor(result_sets)
    def cursor(self): return self.cursor_obj  # type: ignore[no-untyped-def]


def test_postgres_metadata_collector_supplies_family_models() -> None:
    conn = FakeMetadataConnection([
        [("demo",)], [("demo", "accounts", "r")],
        [("demo", "accounts", "email", "text")], [("demo", "decode_event", "jsonb")],
    ])
    snapshot = load_postgres_metadata(conn, max_rows=10, schemas=("demo",))
    assert snapshot == MetadataSnapshot(
        schemas=["demo"],
        objects=[CompletionObject("accounts", "demo", "table")],
        columns=[CompletionColumn("accounts", "email", "demo", "text")],
        functions=[CompletionObject("decode_event", "demo", "function", "jsonb")],
    )
    assert all("= ANY(%s)" in query for query in conn.cursor_obj.executed)


def test_postgres_metadata_limit_remains_provider_specific() -> None:
    conn = FakeMetadataConnection([[("public",), ("demo",), ("extra",)], [], [], []])
    with pytest.raises(MetadataLimitExceeded, match="reaching 2 rows"):
        load_postgres_metadata(conn, max_rows=2)
