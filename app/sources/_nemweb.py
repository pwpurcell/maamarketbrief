"""Shared parsing helpers for NEMWeb-style I/D/C row CSVs.

Format used by AEMO dispatch and GSH reports:

    C,NEMP.WORLD,REPORT_NAME,...                       # comment / metadata
    I,SOURCE,TABLE,VER,col1,col2,...                   # schema for a table
    D,SOURCE,TABLE,VER,val1,val2,...                   # data row
    ...
    C,END OF REPORT,...

A single file can contain multiple tables, each preceded by its own I row.
This helper extracts one named table as a DataFrame."""

from __future__ import annotations

import csv

import pandas as pd


def parse_aemo_idr_csv(text: str, table_name: str) -> pd.DataFrame:
    """Extract one table from a NEMWeb I/D/C CSV.

    Matches `table_name` case-insensitively against the I row's table column
    (parts[2]). Returns a DataFrame with the I row's column names and string
    cell values; callers cast columns to the types they need."""
    target = table_name.upper()
    headers: list[str] | None = None
    rows: list[list[str]] = []

    for raw in text.splitlines():
        if not raw or raw[0] not in ("I", "D"):
            continue
        parts = next(csv.reader([raw]))
        if len(parts) < 4:
            continue
        if parts[0] == "I" and parts[2].upper() == target:
            headers = parts[4:]
        elif parts[0] == "D" and parts[2].upper() == target and headers:
            data = parts[4:4 + len(headers)]
            if len(data) == len(headers):
                rows.append(data)

    if headers is None:
        raise ValueError(f"No I row found for table {table_name!r}")
    return pd.DataFrame(rows, columns=headers)
