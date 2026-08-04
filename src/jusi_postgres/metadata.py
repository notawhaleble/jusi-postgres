from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .state import read_json, write_json


METADATA_TTL_SECONDS = 60 * 60.0


class MetadataLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class CompletionObject:
    name: str
    schema: str
    kind: str
    detail: str = ""


@dataclass(frozen=True)
class CompletionColumn:
    schema: str
    table: str
    name: str
    data_type: str = ""


@dataclass
class MetadataSnapshot:
    schemas: list[str] = field(default_factory=list)
    objects: list[CompletionObject] = field(default_factory=list)
    columns: list[CompletionColumn] = field(default_factory=list)
    functions: list[CompletionObject] = field(default_factory=list)
    refreshed_at: float = 0.0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MetadataSnapshot":
        return cls(
            schemas=[str(item) for item in payload.get("schemas", []) if str(item)],
            objects=[
                CompletionObject(
                    name=str(item.get("name", "")),
                    schema=str(item.get("schema", "")),
                    kind=str(item.get("kind", "")),
                    detail=str(item.get("detail", "")),
                )
                for item in payload.get("objects", [])
                if isinstance(item, dict) and item.get("name")
            ],
            columns=[
                CompletionColumn(
                    schema=str(item.get("schema", "")),
                    table=str(item.get("table", "")),
                    name=str(item.get("name", "")),
                    data_type=str(item.get("data_type", "")),
                )
                for item in payload.get("columns", [])
                if isinstance(item, dict) and item.get("table") and item.get("name")
            ],
            functions=[
                CompletionObject(
                    name=str(item.get("name", "")),
                    schema=str(item.get("schema", "")),
                    kind="function",
                    detail=str(item.get("detail", "")),
                )
                for item in payload.get("functions", [])
                if isinstance(item, dict) and item.get("name")
            ],
            refreshed_at=float(payload.get("refreshed_at", 0.0) or 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemas": self.schemas,
            "objects": [item.__dict__ for item in self.objects],
            "columns": [item.__dict__ for item in self.columns],
            "functions": [item.__dict__ for item in self.functions],
            "refreshed_at": self.refreshed_at,
        }


class MetadataCache:
    def __init__(
        self,
        cache_dir: Path,
        loader: Callable[[], MetadataSnapshot],
        *,
        on_warning: Callable[[str], None] | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_path = cache_dir / "metadata.json"
        self.loader = loader
        self.on_warning = on_warning
        self._lock = threading.Lock()
        self._refreshing = False
        self._thread: threading.Thread | None = None
        self._snapshot = MetadataSnapshot.from_dict(read_json(self.cache_path))

    def snapshot(self) -> MetadataSnapshot:
        with self._lock:
            return self._snapshot

    def mark_stale(self) -> None:
        with self._lock:
            self._snapshot.refreshed_at = 0.0

    def ensure_fresh_async(self, *, force: bool = False) -> bool:
        with self._lock:
            if self._refreshing:
                return False
            age = time.time() - self._snapshot.refreshed_at if self._snapshot.refreshed_at else 10**9
            if not force and age < METADATA_TTL_SECONDS:
                return False
            self._refreshing = True
        thread = threading.Thread(target=self._refresh, daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return True

    def close(self, *, timeout: float = 2.0) -> bool:
        with self._lock:
            thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def _refresh(self) -> None:
        try:
            try:
                snapshot = self.loader()
            except MetadataLimitExceeded as exc:
                if self.on_warning is not None:
                    self.on_warning(str(exc))
                return
            except Exception as exc:
                if self.on_warning is not None:
                    self.on_warning(f"PostgreSQL metadata collection failed: {exc.__class__.__name__}: {exc}")
                return
            else:
                snapshot.refreshed_at = time.time()
                write_json(self.cache_path, snapshot.to_dict())
                with self._lock:
                    self._snapshot = snapshot
        finally:
            with self._lock:
                self._refreshing = False


def load_postgres_metadata(
    conn: Any,
    *,
    max_rows: int = 1_000_000,
    schemas: Sequence[str] = (),
) -> MetadataSnapshot:
    collector = _BoundedMetadataCollector(max_rows=max_rows, schemas=schemas)
    with conn.cursor() as cur:
        cur.execute(*collector.query("SELECT nspname FROM pg_namespace", "nspname"))
        schema_names = sorted(str(row[0]) for row in collector.fetch(cur, "schemas"))
        cur.execute(
            *collector.query(
                """
                SELECT n.nspname, c.relname, c.relkind
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                """,
                "n.nspname",
                extra_predicates=("c.relkind IN ('r', 'p', 'v', 'm', 'f')",),
            )
        )
        objects = [
            CompletionObject(schema=str(row[0]), name=str(row[1]), kind=_relation_kind(str(row[2])))
            for row in collector.fetch(cur, "relations")
        ]
        objects.sort(key=lambda item: (item.schema, item.name, item.kind))
        cur.execute(
            *collector.query(
                """
                SELECT table_schema, table_name, column_name, data_type
                FROM information_schema.columns
                """,
                "table_schema",
            )
        )
        columns = [
            CompletionColumn(schema=str(row[0]), table=str(row[1]), name=str(row[2]), data_type=str(row[3]))
            for row in collector.fetch(cur, "columns")
        ]
        columns.sort(key=lambda item: (item.schema, item.table, item.name))
        cur.execute(
            *collector.query(
                """
                SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                """,
                "n.nspname",
            )
        )
        functions = [
            CompletionObject(schema=str(row[0]), name=str(row[1]), kind="function", detail=str(row[2]))
            for row in collector.fetch(cur, "functions")
        ]
        functions.sort(key=lambda item: (item.schema, item.name, item.detail))
    return MetadataSnapshot(schemas=schema_names, objects=objects, columns=columns, functions=functions)


class _BoundedMetadataCollector:
    def __init__(self, *, max_rows: int, schemas: Sequence[str]) -> None:
        self.max_rows = max_rows
        self.schemas = tuple(schema for schema in schemas if schema)
        self.collected = 0

    def query(
        self,
        base_query: str,
        schema_column: str,
        *,
        extra_predicates: Sequence[str] = (),
    ) -> tuple[str, tuple[Any, ...]]:
        predicates = list(extra_predicates)
        params: list[Any] = []
        predicates.append(f"{schema_column} NOT LIKE %s")
        params.append("pg_toast%")
        if self.schemas:
            predicates.append(f"{schema_column} = ANY(%s)")
            params.append(list(self.schemas))
        remaining = self.max_rows - self.collected + 1
        return f"{base_query} WHERE {' AND '.join(predicates)} LIMIT %s", (*params, remaining)

    def fetch(self, cursor: Any, label: str) -> list[Any]:
        remaining = self.max_rows - self.collected
        rows = list(cursor.fetchmany(remaining + 1))
        if len(rows) > remaining:
            schema_hint = ""
            if self.schemas:
                schema_hint = f" Current filter: {', '.join(self.schemas)}."
            raise MetadataLimitExceeded(
                f"PostgreSQL metadata collection stopped after reaching {self.max_rows} rows while loading {label}."
                f"{schema_hint} Schema filtering is required: add or narrow metadata_schemas,"
                " raise metadata_max_rows, or disable metadata."
            )
        self.collected += len(rows)
        return rows


def relation_names(snapshot: MetadataSnapshot) -> Iterable[tuple[str, str]]:
    for item in snapshot.objects:
        yield item.schema, item.name


def _relation_kind(value: str) -> str:
    return {
        "r": "table",
        "p": "table",
        "v": "view",
        "m": "view",
        "f": "foreign table",
    }.get(value, "relation")
