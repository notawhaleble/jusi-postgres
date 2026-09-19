from __future__ import annotations

from typing import Any, Iterable, Sequence

from jusi_sql import CompletionColumn, CompletionObject, MetadataSnapshot


class MetadataLimitExceeded(RuntimeError):
    pass


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
