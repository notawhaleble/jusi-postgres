from __future__ import annotations

import json
import curses
from pathlib import Path
import re
import sys
import tempfile
import threading
import uuid
from collections import deque
from typing import Any

import visidata
from visidata import ItemColumn, SequenceSheet, run, vd

from jusi_sql import (
    MetadataCache,
    MetadataSnapshot,
    SqlCompletionRequest,
    SqlSheetActions,
    bind_sql_actions,
    complete_sql,
    install_visidata_commands,
    sql_cache_directory,
)

from .config import DEFAULT_METADATA_MAX_ROWS, kerberos_cache_env, parse_postgres_options
from .constants import POSTGRES_BOOTSTRAP_SQL
from .ipc import ApplicationController
from .metadata import load_postgres_metadata


RESULT_QUERY_PREFIXES = ("select", "with", "values", "table")
POSTGRES_KEYWORDS = (
    "SELECT", "FROM", "WHERE", "JOIN", "LEFT", "RIGHT", "FULL", "INNER", "OUTER",
    "ON", "GROUP", "BY", "ORDER", "HAVING", "LIMIT", "OFFSET", "INSERT", "INTO",
    "VALUES", "UPDATE", "SET", "DELETE", "RETURNING", "WITH", "AS", "DISTINCT",
    "CREATE", "ALTER", "DROP", "TABLE", "VIEW", "INDEX", "BEGIN", "COMMIT", "ROLLBACK",
)
POSTGRES_RELATION_KEYWORDS = frozenset({
    "FROM", "JOIN", "INTO", "UPDATE", "TABLE", "VIEW", "DESCRIBE", "DESC",
})
_PENDING_SHEETS: deque[Any] = deque()


class RawBinaryValue:
    __slots__ = ("value",)

    def __init__(self, value: bytes | bytearray | memoryview) -> None:
        self.value = value

    @property
    def size(self) -> int:
        if isinstance(self.value, memoryview):
            return self.value.nbytes
        return len(self.value)

    def tobytes(self) -> bytes:
        if isinstance(self.value, memoryview):
            return self.value.tobytes()
        if isinstance(self.value, bytearray):
            return bytes(self.value)
        return self.value

    def __str__(self) -> str:
        return f"<binary data: {self.size} bytes>"

    def __repr__(self) -> str:
        return str(self)


class PostgresSession:
    def __init__(
        self,
        *,
        alias: str,
        connect_options: dict[str, Any],
        krb5ccname: str = "",
        initial_fetch: int = 100,
        collect_metadata: bool = True,
        metadata_max_rows: int = DEFAULT_METADATA_MAX_ROWS,
        metadata_schemas: tuple[str, ...] = (),
    ) -> None:
        self.alias = alias
        self.connect_options = connect_options
        self.krb5ccname = krb5ccname
        self.initial_fetch = initial_fetch
        self.collect_metadata = collect_metadata
        self.metadata_max_rows = metadata_max_rows
        self.metadata_schemas = metadata_schemas
        self.conn: Any = None
        self.metadata_conn: Any = None
        self.metadata_conn_lock = threading.Lock()
        self.metadata_warning_lock = threading.Lock()
        self.metadata_warnings: list[str] = []
        self.lock = threading.RLock()
        self.notices: list[str] = []
        self.sheets: list[PostgresResultSheet] = []
        metadata_cache_options = dict(connect_options)
        metadata_cache_options["__metadata_max_rows"] = metadata_max_rows
        metadata_cache_options["__metadata_schemas"] = metadata_schemas
        self.metadata = MetadataCache(
            sql_cache_directory("postgres", alias, metadata_cache_options),
            self._load_metadata,
            on_warning=self._metadata_warning,
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

    def complete(self, request: SqlCompletionRequest) -> dict[str, Any]:
        self.show_metadata_warnings()
        snapshot = MetadataSnapshot() if not self.collect_metadata else self.metadata.snapshot()
        return _complete_postgres_sql(snapshot, request)

    def enter_cell(self) -> None:
        if not self.collect_metadata:
            return
        self.metadata.ensure_fresh_async()

    def followup(self, body: str) -> None:
        sql = _followup_sql(body).strip()
        if not sql:
            return
        self.enter_cell()
        sheet = PostgresResultSheet(session=self, query=sql)
        _queue_sheet(sheet)

    def interrupt(self) -> None:
        conn = self.conn
        if conn is None or conn.closed:
            return
        try:
            conn.cancel()
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
            self._cancel_metadata()
        for sheet in list(self.sheets):
            sheet.close_cursor()
        conn = self.conn
        if conn is not None and not conn.closed:
            conn.close()

    def _load_metadata(self) -> Any:
        with kerberos_cache_env(self.krb5ccname):
            conn = _connect_psycopg(dict(self.connect_options))
        with self.metadata_conn_lock:
            self.metadata_conn = conn
        try:
            try:
                conn.autocommit = True
            except Exception:
                pass
            return load_postgres_metadata(
                conn,
                max_rows=self.metadata_max_rows,
                schemas=self.metadata_schemas,
            )
        finally:
            with self.metadata_conn_lock:
                self.metadata_conn = None
            try:
                conn.close()
            except Exception:
                pass

    def _cancel_metadata(self) -> None:
        with self.metadata_conn_lock:
            conn = self.metadata_conn
        if conn is None or conn.closed:
            return
        try:
            conn.cancel()
            vd.warning("PostgreSQL metadata collection cancellation requested")
        except Exception as exc:
            vd.warning(f"PostgreSQL metadata cancellation failed: {exc}")

    def _metadata_warning(self, message: str) -> None:
        with self.metadata_warning_lock:
            self.metadata_warnings.append(message)

    def show_metadata_warnings(self) -> None:
        with self.metadata_warning_lock:
            warnings = list(self.metadata_warnings)
            self.metadata_warnings.clear()
        for message in warnings:
            vd.warning(message)

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


def _complete_postgres_sql(
    snapshot: MetadataSnapshot,
    request: SqlCompletionRequest,
) -> dict[str, list[dict[str, Any]]]:
    if _is_blank_relation_context(request.prefix):
        snapshot = MetadataSnapshot(
            schemas=list(snapshot.schemas),
            refreshed_at=snapshot.refreshed_at,
        )
    return complete_sql(
        snapshot,
        request,
        keywords=POSTGRES_KEYWORDS,
        relation_keywords=POSTGRES_RELATION_KEYWORDS,
    )


def _is_blank_relation_context(prefix: str) -> bool:
    if not prefix or not prefix[-1].isspace():
        return False
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", prefix)
    return bool(words and words[-1].upper() in POSTGRES_RELATION_KEYWORDS)


class PostgresResultSheet(SequenceSheet):
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
        bind_sql_actions(self, SqlSheetActions(
            fetch_more=self.fetch_more,
            commit=session.commit,
            rollback=session.rollback,
        ))

    def iterload(self):  # type: ignore[no-untyped-def]
        try:
            if _looks_like_result_query(self.query):
                yield from self._load_result_query()
            else:
                yield from self._load_statement()
        except Exception as exc:
            vd.exceptionCaught(exc)
            vd.warning(f"PostgreSQL query failed: {exc.__class__.__name__}: {exc}; press Ctrl-E for details")

    def fetch_more(self, count: int) -> int:
        if self.cursor_closed_reason:
            vd.warning(f"PostgreSQL cursor is closed: {self.cursor_closed_reason}")
            return 0
        if self.exhausted or self.cursor is None:
            vd.status("PostgreSQL cursor is exhausted")
            return 0
        try:
            with self.session.lock:
                rows = self._fetch_rows(count)
            for row in rows:
                self.addRow(_display_row(row))
            self._notify_more()
            return len(rows)
        except Exception as exc:
            vd.warning(f"PostgreSQL fetch failed: {exc}")
            return 0

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
        if description:
            yield column_names
        for row in rows:
            yield _display_row(row)
        for notice in self.session.pop_notices():
            vd.status(f"PostgreSQL notice: {notice}")
        self.session.show_metadata_warnings()
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
                        yield _display_row(row)
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
        self.session.show_metadata_warnings()

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


def install_postgres_commands() -> None:
    install_visidata_commands(visidata)
    if getattr(visidata.BaseSheet, "_jusi_postgres_commands_v1", False):
        return

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

    @visidata.BaseSheet.command("", "jusi-postgres-open-pending-sheet", "open pending PostgreSQL result", replay=False)
    def _open_pending(_sheet: Any) -> None:
        if not _PENDING_SHEETS:
            return
        next_sheet = _PENDING_SHEETS.popleft()
        vd.push(next_sheet)
        next_sheet.ensureLoaded()

    setattr(visidata.BaseSheet, "_jusi_postgres_commands_v1", True)


def _initialize_visidata_application() -> None:
    from jusi.visidata_support import initialize_visidata

    initialize_visidata(open_name="selection.sql", open_filetype="sql")
    install_postgres_commands()


def _queue_sheet(sheet: PostgresResultSheet) -> None:
    _PENDING_SHEETS.append(sheet)
    vd.queueCommand("jusi-postgres-open-pending-sheet")
    try:
        curses.ungetch(curses.KEY_RESIZE)
    except Exception:
        pass


def _followup_sql(body: str) -> str:
    first, separator, remainder = body.partition("\n")
    header = first.strip()
    if header == "%%sql" or header.startswith(("%%sql ", "%%sql\t")):
        return remainder if separator else ""
    return body


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
    if isinstance(value, RawBinaryValue):
        value = value.tobytes()
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


def _display_row(row: Any) -> list[Any]:
    return [_display_cell(value) for value in row]


def _display_cell(value: Any) -> Any:
    if isinstance(value, RawBinaryValue):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return RawBinaryValue(value)
    return value


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
    conn = psycopg.connect(**connect_options)
    _install_infinity_timestamp_loaders(conn)
    return conn


def _install_infinity_timestamp_loaders(conn: Any) -> None:
    try:
        from psycopg.types.datetime import DateLoader, TimestampLoader, TimestamptzLoader
    except Exception:
        return

    class DateInfinityLoader(DateLoader):  # type: ignore[misc, valid-type]
        def load(self, data: Any) -> Any:
            text = bytes(data).decode("ascii")
            if text in ("infinity", "-infinity"):
                return text
            return super().load(data)

    class TimestampInfinityLoader(TimestampLoader):  # type: ignore[misc, valid-type]
        def load(self, data: Any) -> Any:
            text = bytes(data).decode("ascii")
            if text in ("infinity", "-infinity"):
                return text
            return super().load(data)

    class TimestamptzInfinityLoader(TimestamptzLoader):  # type: ignore[misc, valid-type]
        def load(self, data: Any) -> Any:
            text = bytes(data).decode("ascii")
            if text in ("infinity", "-infinity"):
                return text
            return super().load(data)

    conn.adapters.register_loader("date", DateInfinityLoader)
    conn.adapters.register_loader("timestamp", TimestampInfinityLoader)
    conn.adapters.register_loader("timestamptz", TimestamptzInfinityLoader)


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)
    if not isinstance(value, dict):
        raise RuntimeError("invalid PostgreSQL application payload")
    return value


def _handle_application_operation(session: PostgresSession, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    if operation == "followup":
        body = payload.get("body")
        if not isinstance(body, str):
            raise ValueError("SQL followup requires string body")
        session.followup(body)
        return {"accepted": True}
    if operation == "complete":
        return session.complete(SqlCompletionRequest.from_payload(payload))
    if operation == "interrupt":
        session.interrupt()
        return {"accepted": True}
    raise ValueError(f"unsupported PostgreSQL application operation: {operation}")


def run_postgres_application(payload_path: Path, socket_path: str) -> int:
    session: PostgresSession | None = None
    try:
        _initialize_visidata_application()
        visidata.vd.timeouts_before_idle = -1
        payload = _read_payload(payload_path)
        query = str(payload.get("sql", "")).strip() or POSTGRES_BOOTSTRAP_SQL
        alias = str(payload.get("alias", "")).strip()
        if not alias:
            raise RuntimeError("missing SQL target alias")
        raw_options = payload.get("options")
        if not isinstance(raw_options, dict):
            raise RuntimeError("missing PostgreSQL target options")
        options = parse_postgres_options(raw_options)
        session = PostgresSession(
            alias=alias,
            connect_options=options.connect,
            krb5ccname=options.krb5ccname,
            initial_fetch=options.initial_fetch,
            collect_metadata=options.collect_metadata,
            metadata_max_rows=options.metadata_max_rows,
            metadata_schemas=options.metadata_schemas,
        )
        controller = ApplicationController(
            socket_path,
            lambda operation, control_payload: _handle_application_operation(session, operation, control_payload),
        )
        controller.start()
        session.enter_cell()
        sheet = PostgresResultSheet(session=session, query=query)
        run(sheet)
        return 0
    except Exception as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.stderr.flush()
        return 2
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--application":
        raise SystemExit("PostgreSQL application requires a private payload path and control socket")
    raise SystemExit(run_postgres_application(Path(sys.argv[2]), sys.argv[3]))
