"""Open Office workbooks from bytes without consulting the path suffix."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl


def load_workbook_by_content(path: Path, **kwargs: Any) -> openpyxl.Workbook:
    """Load an OOXML workbook and keep its source stream alive until close()."""
    handle = path.open("rb")
    try:
        workbook = openpyxl.load_workbook(handle, **kwargs)
    except Exception:
        handle.close()
        raise

    close_workbook = workbook.close

    def close() -> None:
        try:
            close_workbook()
        finally:
            handle.close()

    workbook.close = close  # type: ignore[method-assign]
    return workbook
