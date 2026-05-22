from __future__ import annotations

from jusi.plugins import DisplayHandlerSpec, MagicCommand
from jusi_sql import BaseSqlHandler

from .constants import POSTGRES_BOOTSTRAP_SQL


class PostgresHandler(BaseSqlHandler):
    def handler_id(self) -> str:
        return "postgres"

    @staticmethod
    def bootstrap_cell_body(first_line: str) -> str | None:
        _ = first_line
        return POSTGRES_BOOTSTRAP_SQL

    def plugin_runtime_callable(self) -> str:
        return "jusi_postgres.runner:run_postgres_runner"


def display_handler_specs() -> tuple[DisplayHandlerSpec, ...]:
    return (
        DisplayHandlerSpec(
            handler_id="postgres",
            factory=PostgresHandler,
            magic_commands=(MagicCommand("sql", bootstrap_body=PostgresHandler.bootstrap_cell_body),),
            kernel_extension_modules=("jusi_postgres.kernel",),
            family_presentation={"syntax": "sql", "indent": "sql", "followup": True, "completion": True},
            presentation={"syntax": "postgresql", "indent": "sql", "followup": True, "completion": True},
        ),
    )
