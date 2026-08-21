import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

from sqlglot import exp

from .compiler import SUPPORTED_LEVELS, compile_check_constraints, emit_outputs, parse_sql, sanitize_identifier


@dataclass
class CaseSpec:
    case_name: str
    fixture_sql: str
    table_name: str
    insert_columns: List[str]
    dataset: str
    workloads: List[Dict[str, str]]


@dataclass
class VariantMetadata:
    label: str
    schema: str
    setup_sql: str
    rewritten_sql: Optional[str] = None
    triggers_sql: Optional[str] = None
    manifest_json: Optional[str] = None


def load_case_spec(case_spec_path: Path | str) -> CaseSpec:
    path = Path(case_spec_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    return CaseSpec(**raw)


def qualify_sql_to_schema(sql: str, schema_name: str) -> str:
    qualified_statements = []

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Table):
            new = node.copy()
            new.set("db", exp.Identifier(this=schema_name, quoted=False))
            return new
        return node

    for statement in parse_sql(sql):
        qualified_statements.append(statement.copy().transform(transform).sql(dialect="postgres"))

    return ";\n\n".join(stmt.rstrip(";") for stmt in qualified_statements) + ";\n"


def wrap_schema_setup(schema_name: str, body_sql: str) -> str:
    cleaned_body = body_sql.strip()
    parts = [
        f"DROP SCHEMA IF EXISTS {schema_name} CASCADE;",
        f"CREATE SCHEMA {schema_name};",
    ]
    if cleaned_body:
        parts.append(cleaned_body if cleaned_body.endswith(";") else f"{cleaned_body};")
    return "\n\n".join(parts) + "\n"


def build_variant_schema(case_name: str, label: str) -> str:
    return sanitize_identifier(f"bench_{case_name}_{label}")


def case_artifact_dir(base_dir: Path | str, case_name: str) -> Path:
    return Path(base_dir) / sanitize_identifier(case_name)


def load_case_metadata(case_dir: Path | str) -> Dict:
    return json.loads((Path(case_dir) / "case.json").read_text(encoding="utf-8"))


def generate_case_artifacts(case_spec_path: Path | str, out_dir: Path | str) -> Path:
    case_spec = load_case_spec(case_spec_path)
    case_spec_file = Path(case_spec_path).resolve()
    fixture_path = (case_spec_file.parent / case_spec.fixture_sql).resolve()
    source_sql = fixture_path.read_text(encoding="utf-8")

    destination = case_artifact_dir(out_dir, case_spec.case_name)
    destination.mkdir(parents=True, exist_ok=True)

    (destination / "source.sql").write_text(source_sql, encoding="utf-8")
    (destination / "case_spec.json").write_text(json.dumps(asdict(case_spec), indent=2), encoding="utf-8")

    metadata = {
        "case_name": case_spec.case_name,
        "fixture_sql": str(fixture_path),
        "table_name": case_spec.table_name,
        "insert_columns": case_spec.insert_columns,
        "dataset": case_spec.dataset,
        "workloads": case_spec.workloads,
        "variants": {},
    }

    native_schema = build_variant_schema(case_spec.case_name, "native")
    native_dir = destination / "native"
    native_dir.mkdir(exist_ok=True)
    native_sql = qualify_sql_to_schema(source_sql, native_schema)
    native_setup = wrap_schema_setup(native_schema, native_sql)
    (native_dir / "setup.sql").write_text(native_setup, encoding="utf-8")
    metadata["variants"]["native"] = asdict(
        VariantMetadata(
            label="native",
            schema=native_schema,
            setup_sql="native/setup.sql",
        )
    )

    for level in SUPPORTED_LEVELS:
        schema_name = build_variant_schema(case_spec.case_name, level.lower())
        level_dir = destination / level.lower()
        level_dir.mkdir(exist_ok=True)

        qualified_input_sql = qualify_sql_to_schema(source_sql, schema_name)
        result = compile_check_constraints(qualified_input_sql)
        rewritten_sql, triggers_sql, manifest_json = emit_outputs(result, level=level)
        setup_sql = wrap_schema_setup(schema_name, "\n\n".join(part for part in [rewritten_sql.strip(), triggers_sql.strip()] if part))

        (level_dir / "rewritten.sql").write_text(rewritten_sql, encoding="utf-8")
        (level_dir / "triggers.sql").write_text(triggers_sql, encoding="utf-8")
        (level_dir / "manifest.json").write_text(manifest_json, encoding="utf-8")
        (level_dir / "setup.sql").write_text(setup_sql, encoding="utf-8")

        metadata["variants"][level] = asdict(
            VariantMetadata(
                label=level,
                schema=schema_name,
                setup_sql=f"{level.lower()}/setup.sql",
                rewritten_sql=f"{level.lower()}/rewritten.sql",
                triggers_sql=f"{level.lower()}/triggers.sql",
                manifest_json=f"{level.lower()}/manifest.json",
            )
        )

    (destination / "case.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return destination
