"""Workflow schemas and the disposition catalogue, loaded dynamically from the schema files."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.normalizers import normalize_header

FieldType = Literal["string", "phone", "date", "decimal", "enum"]


class SchemaError(ValueError):
    """The schema files are inconsistent or unreadable."""


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: FieldType
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: list[str] = Field(default_factory=list)
    values: list[str] | None = None
    default: str | None = None


class WorkflowSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    description: str = ""
    fields: list[FieldSpec]

    @model_validator(mode="after")
    def _check(self) -> "WorkflowSchema":
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.workflow_id}: duplicate field names")
        refs = [f for f in self.fields if f.reference]
        if len(refs) != 1:
            raise ValueError(f"{self.workflow_id}: exactly one field must have reference=true")
        if refs[0].pii:
            raise ValueError(f"{self.workflow_id}: the reference field cannot be PII (it appears in reports)")
        phones = [f for f in self.fields if f.type == "phone"]
        if not phones or not phones[0].required:
            raise ValueError(f"{self.workflow_id}: first phone field must exist and be required")
        for f in self.fields:
            if f.type == "enum" and not f.values:
                raise ValueError(f"{self.workflow_id}.{f.name}: enum needs values")
        return self

    @property
    def reference_field(self) -> FieldSpec:
        return next(f for f in self.fields if f.reference)

    @property
    def identity_phone_field(self) -> FieldSpec:
        return next(f for f in self.fields if f.type == "phone")

    @property
    def encrypted_field_names(self) -> set[str]:
        """Fields stored only inside the encrypted blob: every ``pii: true`` field, plus the
        identity phone even if a schema forgot to flag it (a phone number is never stored in clear)."""
        return {f.name for f in self.fields if f.pii} | {self.identity_phone_field.name}

    def header_lookup(self) -> dict[str, str]:
        """Map normalised header text -> field name (name and aliases, same normalisation)."""
        lookup: dict[str, str] = {}
        for spec in self.fields:
            for label in [spec.name, *spec.aliases]:
                key = normalize_header(label)
                if lookup.get(key, spec.name) != spec.name:
                    raise SchemaError(f"{self.workflow_id}: header '{key}' maps to two fields")
                lookup[key] = spec.name
        return lookup


@dataclass(frozen=True)
class Disposition:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


@dataclass
class Catalogue:
    """All workflows plus their disposition codes."""

    workflows: dict[str, WorkflowSchema]
    dispositions: dict[str, dict[str, Disposition]] = field(default_factory=dict)

    def is_known(self, workflow_id: str, code: str) -> bool:
        return code in self.dispositions.get(workflow_id, {})

    def is_terminal(self, workflow_id: str, code: str) -> bool:
        return self.dispositions[workflow_id][code].is_terminal

    def is_success(self, workflow_id: str, code: str) -> bool:
        return self.dispositions[workflow_id][code].is_success


def _parse_bool(text: str) -> bool:
    value = text.strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    raise SchemaError(f"cannot parse boolean from '{text}'")


def load_catalogue(schemas_dir: Path) -> Catalogue:
    """Load every ``*.json`` workflow schema and ``dispositions.csv`` from ``schemas_dir``."""
    if not schemas_dir.is_dir():
        raise SchemaError(f"schemas directory not found: {schemas_dir}")
    workflows: dict[str, WorkflowSchema] = {}
    for path in sorted(schemas_dir.glob("*.json")):
        try:
            schema = WorkflowSchema.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except ValueError as exc:
            raise SchemaError(f"invalid schema {path.name}: {exc}") from exc
        if schema.workflow_id in workflows:
            raise SchemaError(f"duplicate workflow_id {schema.workflow_id}")
        schema.header_lookup()  # fail fast on ambiguous aliases
        workflows[schema.workflow_id] = schema
    if not workflows:
        raise SchemaError(f"no workflow schemas found in {schemas_dir}")

    dispositions: dict[str, dict[str, Disposition]] = {w: {} for w in workflows}
    csv_path = schemas_dir / "dispositions.csv"
    if not csv_path.is_file():
        raise SchemaError("dispositions.csv not found")
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            wf = row["workflow_id"].strip()
            if wf not in workflows:
                raise SchemaError(f"dispositions.csv references unknown workflow {wf}")
            code = row["code"].strip()
            dispositions[wf][code] = Disposition(
                wf, code, row["label"].strip(), _parse_bool(row["is_terminal"]), _parse_bool(row["is_success"])
            )
    return Catalogue(workflows, dispositions)
