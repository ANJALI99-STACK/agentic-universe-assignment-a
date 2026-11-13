"""Pure lead-file logic: read a CSV against a workflow schema and validate each row.

No database access here, so every rule is directly unit-testable.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from src.normalizers import InvalidValue, normalize_header, parse_field_value
from src.schemas import WorkflowSchema


@dataclass
class RowResult:
    """Outcome of validating one non-blank data row."""

    row_number: int
    rejection_reason: str | None = None
    values: dict[str, str | None] = field(default_factory=dict, repr=False)  # contains PII: never log
    warnings: int = 0

    @property
    def rejected(self) -> bool:
        return self.rejection_reason is not None


def map_columns(header: list[str], schema: WorkflowSchema) -> dict[str, int]:
    """Map schema field name -> column index. Unmatched columns are ignored; first match wins."""
    lookup = schema.header_lookup()
    mapping: dict[str, int] = {}
    for index, cell in enumerate(header):
        field_name = lookup.get(normalize_header(cell))
        if field_name is not None and field_name not in mapping:
            mapping[field_name] = index
    return mapping


def iter_raw_rows(path: Path, schema: WorkflowSchema) -> Iterator[tuple[int, dict[str, str | None]]]:
    """Yield ``(row_number, {field_name: trimmed value or None})`` for every non-blank data row.

    Row numbers count non-blank data rows only (header excluded), starting at 1.
    Fully blank rows are skipped and not counted. Empty values become ``None``.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        mapping: dict[str, int] | None = None
        row_number = 0
        for cells in reader:
            if not any(cell.strip() for cell in cells):
                continue  # blank row (also covers a completely empty line)
            if mapping is None:  # first non-blank row is the header
                mapping = map_columns(cells, schema)
                continue
            row_number += 1
            raw: dict[str, str | None] = {}
            for field_name, index in mapping.items():
                value = cells[index].strip() if index < len(cells) else ""
                raw[field_name] = value or None
            yield row_number, raw


def validate_row(schema: WorkflowSchema, row_number: int, raw: dict[str, str | None]) -> RowResult:
    """Walk the schema fields in declared order; the first failure is the rejection reason.

    * required field missing            -> ``missing_required:<field>``
    * required field present but invalid -> ``invalid_<type>`` (``invalid_phone`` per the spec)
    * optional field present but invalid -> value ``None`` and one warning
    Warnings of a rejected row are discarded (they only count for rows that are kept).
    """
    result = RowResult(row_number)
    warnings = 0
    for spec in schema.fields:
        text = raw.get(spec.name)
        if text is None:
            if spec.required:
                result.rejection_reason = f"missing_required:{spec.name}"
                result.values = {}
                return result
            result.values[spec.name] = None
            continue
        try:
            result.values[spec.name] = parse_field_value(spec, text)
        except InvalidValue:
            if spec.required:
                result.rejection_reason = f"invalid_{spec.type}"
                result.values = {}
                return result
            result.values[spec.name] = None
            warnings += 1
    result.warnings = warnings
    return result
