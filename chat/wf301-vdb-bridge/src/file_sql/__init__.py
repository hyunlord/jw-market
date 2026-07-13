"""Feature-gated, session-scoped SQLite query APIs for uploaded files."""

from .config import FileSqlConfig
from .errors import (
    FileSqlDisabledError,
    FileSqlNotFoundError,
    FileSqlRejectedError,
)
from .models import FileSqlInventory, FileSqlResult, ProvisionedSchema
from .policy import _sqlite_authorizer
from .service import (
    FileSqlService,
    describe_schema_for_llm,
    drop_logical_table,
    drop_session_tables,
    provision_session_table,
    run_scoped_query,
)

__all__ = [
    "FileSqlConfig",
    "FileSqlDisabledError",
    "FileSqlInventory",
    "FileSqlNotFoundError",
    "FileSqlRejectedError",
    "FileSqlResult",
    "FileSqlService",
    "ProvisionedSchema",
    "_sqlite_authorizer",
    "describe_schema_for_llm",
    "drop_logical_table",
    "drop_session_tables",
    "provision_session_table",
    "run_scoped_query",
]
