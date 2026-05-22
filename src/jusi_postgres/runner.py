from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import threading
import uuid
from typing import Any

import visidata
from visidata import ItemColumn, run, vd

from jusi.infrastructure.debug_timing import emit_timing
from jusi.visidata_support import bind_visidata_runtime, set_plugin_execution_status
from jusi_sql import BaseSqlSheet, SqlSheetRuntime, install_sql_base_sheet_api, queue_sql_sheet, resolve_sql_target

from .completion import completion_items
from .config import kerberos_cache_env, parse_postgres_options
from .constants import POSTGRES_BOOTSTRAP_SQL
from .metadata import MetadataCache, load_postgres_metadata
from .state import target_cache_dir


RESULT_QUERY_PREFIXES = ("select", "with", "values", "table")


class PostgresSheetRuntime(SqlSheetRuntime):
    def handle_complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"items": list(self.complete(dict(payload)) or ())}

    def handle_followup(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.followup(dict(payload))
        return {}


class PostgresSession:
    def __init__(
        self,
        *,
        alias: str,
        connect_options: dict[str, Any],
        krb5ccname: str = "",
        initial_fetch: int = 100,
    ) -> None:
        self.alias = alias
        self.connect_options = connect_options
        self.krb5ccname = krb5ccname
        self.initial_fetch = initial_fetch
        self.conn: Any = None
        self.lock = threading.RLock()
        self.notices: list[str] = []
        self.sheets: list[PostgresResultSheet] = []
        self.metadata = MetadataCache(
            target_cache_dir(alias, connect_options),
            lambda: self.with_connection(lambda conn: load_postgres_metadata(conn), blocking=False),
        )

    def connect(self) -> Any:
        with self.lock:
            if self.conn is None or self.conn.closed:
                with kerberos_cache_env(self.krb5ccname):
                    self.conn = _connect_psycopg(self.connect_options)
                self.conn.autocommit = False
                try:
                    self.conn.add_notice_handler(self._notice_handler)
                except Exception:
                    pass
                emit_timing("sql.postgres.connection.opened", alias=self.alias)
            return self.conn

    def with_connection(self, fn, *, blocking: bool = True):  # type: ignore[no-untyped-def]
        if not self.lock.acquire(blocking=blocking):
            raise RuntimeError("PostgreSQL connection is busy")
        try:
            conn = self.connect()
            return fn(conn)
        finally:
            self.lock.release()

    def register_sheet(self, sheet: "PostgresResultSheet") -> None:
        self.sheets.append(sheet)

    def complete(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        snapshot = self.metadata.snapshot()
        items = completion_items(snapshot, payload)
        emit_timing("sql.postgres.complete", alias=self.alias, item_count=len(items), current_word=str(payload.get("current_word", "")))
        return items

    def enter_cell(self) -> None:
        started = self.metadata.ensure_fresh_async()
        emit_timing("sql.postgres.metadata.enter_cell", alias=self.alias, refresh_started=started)

    def followup(self, payload: dict[str, Any]) -> None:
        cell_text = str(payload.get("cell_text", "")).strip()
        if not cell_text:
            return
        self.enter_cell()
        sheet = PostgresResultSheet(session=self, query=cell_text)
        bind_postgres_runtime(sheet)
        queue_sql_sheet(sheet)
        emit_timing("sql.postgres.followup", alias=self.alias, query_len=len(cell_text), sheet=sheet.name)

    def interrupt(self) -> None:
        conn = self.conn
        if conn is None or conn.closed:
            return
        try:
            conn.cancel()
            set_plugin_execution_status("interrupted")
            vd.warning("PostgreSQL query cancellation requested")
        except Exception as exc:
            vd.warning(f"PostgreSQL cancellation failed: {exc}")

    def commit(self) -> None:
        conn = self.connect()
        with self.lock:
            conn.commit()
        self._mark_cursors_closed("transaction committed")
        self.metadata.mark_stale()
        vd.status("PostgreSQL transaction committed")

    def rollback(self) -> None:
        conn = self.connect()
        with self.lock:
            conn.rollback()
        self._mark_cursors_closed("transaction rolled back")
        self.metadata.mark_stale()
        vd.status("PostgreSQL transaction rolled back")

    def close(self) -> None:
        metadata_done = self.metadata.close(timeout=2.0)
        if not metadata_done:
            self.interrupt()
        for sheet in list(self.sheets):
            sheet.close_cursor()
        conn = self.conn
        if conn is not None and not conn.closed:
            conn.close()
            emit_timing("sql.postgres.connection.closed", alias=self.alias)

    def pop_notices(self) -> list[str]:
        notices = list(self.notices)
        self.notices.clear()
        return notices

    def _notice_handler(self, diagnostic: Any) -> None:
        message = str(getattr(diagnostic, "message_primary", "") or diagnostic).strip()
        if message:
            self.notices.append(message)

    def _mark_cursors_closed(self, reason: str) -> None:
        for sheet in self.sheets:
            sheet.cursor_closed_reason = reason


class PostgresResultSheet(BaseSqlSheet):
    rowtype = "rows"

    def __init__(self, *, session: PostgresSession, query: str) -> None:
        super().__init__(name=session.alias, source=query)
        self.session = session
        self.query = query
        self.cursor_name = f"jusi_pg_{uuid.uuid4().hex}"
        self.cursor: Any = None
        self.exhausted = False
        self.cursor_closed_reason = ""
        self._buffer: list[Any] = []
        session.register_sheet(self)

    def iterload(self):  # type: ignore[no-untyped-def]
        set_plugin_execution_status("busy")
        emit_timing("sql.postgres.iterload.begin", alias=self.session.alias, query_len=len(self.query))
        try:
            if _looks_like_result_query(self.query):
                yield from self._load_result_query()
            else:
                yield from self._load_statement()
        except Exception as exc:
            self.columns = [ItemColumn("error", 0)]
            yield [f"{exc.__class__.__name__}: {exc}"]
            emit_timing("sql.postgres.iterload.error", alias=self.session.alias, error_type=type(exc).__name__, error=str(exc))
        finally:
            set_plugin_execution_status("follow-up")

    def fetch_more(self, count: int) -> int:
        if self.cursor_closed_reason:
            vd.warning(f"PostgreSQL cursor is closed: {self.cursor_closed_reason}")
            return 0
        if self.exhausted or self.cursor is None:
            vd.status("PostgreSQL cursor is exhausted")
            return 0
        set_plugin_execution_status("busy")
        try:
            with self.session.lock:
                rows = self._fetch_rows(count)
            for row in rows:
                self.addRow(list(row))
            self._notify_more()
            return len(rows)
        except Exception as exc:
            vd.warning(f"PostgreSQL fetch failed: {exc}")
            return 0
        finally:
            set_plugin_execution_status("follow-up")

    def close_cursor(self) -> None:
        cursor = self.cursor
        self.cursor = None
        if cursor is None:
            return
        try:
            cursor.close()
        except Exception:
            pass

    def _load_result_query(self):  # type: ignore[no-untyped-def]
        conn = self.session.connect()
        with self.session.lock:
            self.cursor = conn.cursor(name=self.cursor_name)
            self.cursor.execute(self.query)
            description = self.cursor.description or []
            column_names = [str(item.name) for item in description] if description else ["result"]
            self.columns = [ItemColumn(name, index) for index, name in enumerate(column_names)]
            rows = self._fetch_rows(self.session.initial_fetch)
        emit_timing("sql.postgres.iterload.rows", alias=self.session.alias, row_count=len(rows), column_count=len(column_names))
        if description:
            yield column_names
        for row in rows:
            yield list(row)
        for notice in self.session.pop_notices():
            vd.status(f"PostgreSQL notice: {notice}")
        self._notify_more()

    def _load_statement(self):  # type: ignore[no-untyped-def]
        conn = self.session.connect()
        with self.session.lock:
            with conn.cursor() as cursor:
                cursor.execute(self.query)
                description = cursor.description or []
                if description:
                    column_names = [str(item.name) for item in description]
                    self.columns = [ItemColumn(name, index) for index, name in enumerate(column_names)]
                    rows = cursor.fetchall()
                    yield column_names
                    for row in rows:
                        yield list(row)
                else:
                    self.columns = [ItemColumn("status", 0), ItemColumn("value", 1)]
                    status = str(getattr(cursor, "statusmessage", "") or "done")
                    yield ["status", status]
                    rowcount = int(getattr(cursor, "rowcount", -1) or -1)
                    if rowcount >= 0:
                        yield ["rowcount", rowcount]
                    for notice in self.session.pop_notices():
                        yield ["notice", notice]
                    self.session.metadata.mark_stale()

    def _fetch_rows(self, count: int) -> list[Any]:
        if self.cursor is None:
            return []
        if count == 0:
            rows = list(self._buffer)
            self._buffer.clear()
            rows.extend(self.cursor.fetchall())
            self.exhausted = True
            return rows
        rows = list(self._buffer[:count])
        self._buffer = self._buffer[count:]
        remaining = count - len(rows)
        if remaining > 0:
            fetched = list(self.cursor.fetchmany(remaining + 1))
            rows.extend(fetched[:remaining])
            if len(fetched) > remaining:
                self._buffer.append(fetched[-1])
            else:
                self.exhausted = True
        return rows

    def _notify_more(self) -> None:
        if self._buffer or not self.exhausted:
            vd.warning("PostgreSQL cursor has more rows; press 1-9 or gf to fetch more")
        else:
            vd.status("PostgreSQL cursor exhausted")


def bind_postgres_runtime(sheet: PostgresResultSheet) -> None:
    runtime = PostgresSheetRuntime(
        alias=sheet.session.alias,
        provider="postgres",
        complete=lambda control_payload: sheet.session.complete(control_payload),
        followup=lambda control_payload: sheet.session.followup(control_payload),
        interrupt=sheet.session.interrupt,
        stop=sheet.session.close,
    )
    sheet.bind_sql_runtime(runtime)
    bind_visidata_runtime(sheet, runtime)


def install_postgres_commands() -> None:
    install_sql_base_sheet_api()

    for number in range(1, 10):
        command_name = f"jusi-postgres-fetch-{number}"

        def _fetch(sheet: Any, n: int = number) -> None:
            pg_sheet = _postgres_sheet(sheet)
            if pg_sheet is not None:
                pg_sheet.fetch_more(n)

        visidata.BaseSheet.command(str(number), command_name, f"fetch {number} PostgreSQL rows", replay=False)(_fetch)

    @visidata.BaseSheet.command("gf", "jusi-postgres-fetch-prompt", "fetch PostgreSQL rows", replay=False)
    def _fetch_prompt(sheet: Any) -> None:
        pg_sheet = _postgres_sheet(sheet)
        if pg_sheet is None:
            return
        raw = vd.input("fetch rows (0 for all): ")
        try:
            count = int(str(raw).strip())
        except ValueError:
            vd.warning("Fetch count must be a number")
            return
        if count < 0:
            vd.warning("Fetch count must be >= 0")
            return
        pg_sheet.fetch_more(count)

    @visidata.BaseSheet.command("gc", "jusi-postgres-commit", "commit PostgreSQL transaction", replay=False)
    def _commit(sheet: Any) -> None:
        pg_sheet = _postgres_sheet(sheet)
        if pg_sheet is not None:
            pg_sheet.session.commit()

    @visidata.BaseSheet.command("gr", "jusi-postgres-rollback", "roll back PostgreSQL transaction", replay=False)
    def _rollback(sheet: Any) -> None:
        pg_sheet = _postgres_sheet(sheet)
        if pg_sheet is not None:
            pg_sheet.session.rollback()

    @visidata.BaseSheet.command("gb", "jusi-postgres-open-raw-value", "open raw PostgreSQL cell value", replay=False)
    def _open_raw_value(sheet: Any) -> None:
        value = getattr(sheet, "cursorValue", None)
        if callable(value):
            value = value()
        value = value if value is not None else ""
        extension = str(vd.input("extension: ") or "").strip().lstrip(".")
        suffix = f".{extension}" if extension else ""
        path = _write_raw_value_file(value, suffix)
        opened = vd.openPath(visidata.Path(path))
        vd.push(opened)


def _postgres_sheet(sheet: Any) -> PostgresResultSheet | None:
    current = sheet
    seen: set[int] = set()
    while current is not None:
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)
        if isinstance(current, PostgresResultSheet):
            return current
        current = getattr(current, "source", None)
    vd.warning("No active PostgreSQL result sheet")
    return None


def _write_raw_value_file(value: Any, suffix: str) -> str:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytearray):
        value = bytes(value)
    if isinstance(value, bytes):
        with tempfile.NamedTemporaryFile("wb", prefix="jusi-postgres-value-", suffix=suffix, delete=False) as handle:
            handle.write(value)
            return handle.name
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="jusi-postgres-value-", suffix=suffix, delete=False) as handle:
        handle.write(_value_to_text(value))
        return handle.name


def _value_to_text(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str, indent=2)
    return "" if value is None else str(value)


def _looks_like_result_query(sql: str) -> bool:
    stripped = sql.lstrip().lower()
    return stripped.startswith(RESULT_QUERY_PREFIXES)


def _connect_psycopg(connect_options: dict[str, Any]) -> Any:
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency 'psycopg'. Install this plugin with dependencies, "
            "for example: ./.venv/bin/python -m pip install -e ."
        ) from exc
    return psycopg.connect(**connect_options)


def _load_payload() -> dict[str, Any]:
    raw = os.environ.get("JUSI_SQL_PAYLOAD_JSON", "").strip()
    if not raw:
        raise RuntimeError("missing JUSI_SQL_PAYLOAD_JSON")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("invalid JUSI_SQL_PAYLOAD_JSON")
    return payload


def _postgres_target_options(alias: str, meta: dict[str, Any]) -> dict[str, object]:
    target_config = meta.get("target_config")
    if isinstance(target_config, dict):
        provider = str(target_config.get("provider", "")).strip()
        if provider and provider != "postgres":
            raise RuntimeError(f"SQL target {alias!r} resolved to provider {provider!r}, not postgres")
        return {str(key): value for key, value in target_config.items()}

    session_config = meta.get("session_config")
    if isinstance(session_config, dict):
        target = resolve_sql_target(alias, config=session_config)
        if target.provider != "postgres":
            raise RuntimeError(f"SQL target {alias!r} resolved to provider {target.provider!r}, not postgres")
        return dict(target.options)

    raise RuntimeError("missing SQL target config")


def _install_signal_handlers(session: PostgresSession) -> None:
    previous = signal.getsignal(signal.SIGINT)

    def _handle_sigint(signum: int, frame: Any) -> None:
        _ = (signum, frame)
        session.interrupt()
        if callable(previous):
            previous(signum, frame)

    signal.signal(signal.SIGINT, _handle_sigint)


def run_postgres_runner() -> int:
    session: PostgresSession | None = None
    try:
        install_postgres_commands()
        visidata.vd.timeouts_before_idle = -1
        payload = _load_payload()
        query = str(payload.get("content", "")).strip()
        meta = payload.get("meta", {})
        if not isinstance(meta, dict):
            raise RuntimeError("invalid SQL payload meta")
        alias = str(meta.get("alias", "")).strip()
        if not alias:
            raise RuntimeError("missing SQL target alias")
        if not query:
            query = POSTGRES_BOOTSTRAP_SQL
        options = parse_postgres_options(_postgres_target_options(alias, meta))
        session = PostgresSession(
            alias=alias,
            connect_options=options.connect,
            krb5ccname=options.krb5ccname,
            initial_fetch=options.initial_fetch,
        )
        _install_signal_handlers(session)
        session.enter_cell()
        sheet = PostgresResultSheet(session=session, query=query)
        bind_postgres_runtime(sheet)
        run(sheet)
        return 0
    except Exception as exc:
        emit_timing("sql.postgres.runner.error", error=str(exc), error_type=exc.__class__.__name__)
        sys.stderr.write(str(exc) + "\n")
        sys.stderr.flush()
        return 2
    finally:
        if session is not None:
            session.close()
