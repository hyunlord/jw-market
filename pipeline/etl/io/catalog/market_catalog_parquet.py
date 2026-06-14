from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


def write_typed_parquet(records: list[dict[str, Any]], output_file: Path, schema: pa.Schema) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(records, schema=schema)
    pq.write_table(table, output_file, compression="zstd", compression_level=3)


def validate_written_schema(output_file: Path, schema: pa.Schema) -> list[dict[str, Any]]:
    table = pq.read_table(output_file)
    if table.schema != schema:
        raise ValueError(f"written schema mismatch:\nexpected={schema}\nactual={table.schema}")
    return table.to_pylist()
