import argparse
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sqlglot
from sqlglot import exp


CHECK_NODE_NAMES = {
    "Check",
    "CheckConstraint",
    "CheckColumnConstraint",
}

UNSUPPORTED_NODE_NAMES = {
    "Select",
    "Subquery",
    "Exists",
    "Window",
    "With",
    "Join",
    "Union",
    "Intersect",
    "Except",
    "Group",
    "Having",
    "Order",
    "Limit",
}

ALLOWED_FUNCTION_NAMES = {
    "abs",
    "length",
}

SUPPORTED_LEVELS = ("A", "B", "BPLUS")

LOCAL_NAMED_CHECK_RE = re.compile(
    r'^\s*CONSTRAINT\s+(?P<name>"?[A-Za-z_][\w$]*"?)\s+CHECK\s*\(.*\)\s*$',
    re.IGNORECASE | re.DOTALL,
)

LOCAL_STANDALONE_CHECK_RE = re.compile(
    r'^\s*(?:CONSTRAINT\s+"?[A-Za-z_][\w$]*"?\s+)?CHECK\s*\(.*\)\s*$',
    re.IGNORECASE | re.DOTALL,
)

DANGLING_NAMED_CONSTRAINT_MIDDLE_RE = re.compile(
    r',\s*CONSTRAINT\s+"?[A-Za-z_][\w$]*"?\s*(?=,)',
    re.IGNORECASE,
)

DANGLING_NAMED_CONSTRAINT_END_RE = re.compile(
    r',\s*CONSTRAINT\s+"?[A-Za-z_][\w$]*"?\s*(?=\))',
    re.IGNORECASE,
)

DANGLING_NAMED_CONSTRAINT_FIRST_MIDDLE_RE = re.compile(
    r'\(\s*CONSTRAINT\s+"?[A-Za-z_][\w$]*"?\s*(?=,)',
    re.IGNORECASE,
)

DANGLING_NAMED_CONSTRAINT_FIRST_END_RE = re.compile(
    r'\(\s*CONSTRAINT\s+"?[A-Za-z_][\w$]*"?\s*(?=\))',
    re.IGNORECASE,
)


class UnsupportedCheckConstraint(Exception):
    pass


@dataclass
class CheckSpec:
    table_key: str
    table_ref: str
    table_base_name: str
    constraint_name: str
    condition_sql: str
    dependent_columns: List[str]
    source_statement_sql: str
    source_kind: str
    synthetic_name: bool = False


@dataclass
class InternalCheckSpec:
    table_key: str
    table_ref: str
    table_base_name: str
    constraint_name: str
    condition: exp.Expression
    condition_sql: str
    dependent_columns: List[str]
    source_statement_sql: str
    source_kind: str
    synthetic_name: bool = False


@dataclass
class CompileResult:
    modified_statements: List[str] = field(default_factory=list)
    checks_by_table: Dict[str, List[InternalCheckSpec]] = field(default_factory=lambda: defaultdict(list))
    warnings: List[str] = field(default_factory=list)


def sanitize_identifier(name: str, max_len: int = 50) -> str:
    safe = re.sub(r"\W+", "_", name).strip("_").lower()
    if not safe:
        safe = "obj"
    return safe[:max_len]


def strip_quotes(identifier: str) -> str:
    if identifier.startswith('"') and identifier.endswith('"') and len(identifier) >= 2:
        return identifier[1:-1]
    return identifier


def sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def parse_sql(sql: str) -> List[exp.Expression]:
    return sqlglot.parse(sql, read="postgres")


def is_check_node(node: exp.Expression) -> bool:
    return node.__class__.__name__ in CHECK_NODE_NAMES


def get_table_expr(statement: exp.Expression) -> Optional[exp.Table]:
    return statement.find(exp.Table)


def get_table_info(statement: exp.Expression) -> Tuple[str, str, str]:
    table = get_table_expr(statement)
    if not table:
        return "table", "table", "table"

    table_ref = table.sql(dialect="postgres")

    parts = []
    if getattr(table, "db", None):
        parts.append(strip_quotes(str(table.db)))
    if getattr(table, "name", None):
        parts.append(strip_quotes(str(table.name)))

    if not parts:
        return "table", "table", "table"

    table_key = ".".join(parts)
    table_base_name = parts[-1]
    return table_key, table_ref, table_base_name


def render_identifier(name: str) -> str:
    return exp.column(name).sql(dialect="postgres")


def render_qualified_identifier(record_name: str, name: str) -> str:
    return exp.column(name, table=record_name).sql(dialect="postgres")


def extract_condition(check_node: exp.Expression) -> exp.Expression:
    condition = getattr(check_node, "this", None)
    if condition is None:
        raise UnsupportedCheckConstraint(f"Cannot extract condition from node: {check_node}")
    return condition.copy()


def has_unsupported_constructs(expr_obj: exp.Expression) -> Optional[str]:
    for node in expr_obj.walk():
        if node.__class__.__name__ in UNSUPPORTED_NODE_NAMES:
            return node.__class__.__name__
    return None


def maybe_function_name(node: exp.Expression) -> Optional[str]:
    class_name = node.__class__.__name__
    lowered = class_name.lower()

    if lowered in ALLOWED_FUNCTION_NAMES:
        return lowered

    if class_name == "Anonymous":
        name = getattr(node, "name", None)
        if name:
            return str(name).lower()

    return None


def validate_supported_expr(expr_obj: exp.Expression) -> None:
    bad = has_unsupported_constructs(expr_obj)
    if bad:
        raise UnsupportedCheckConstraint(
            f"Unsupported construct in CHECK expression: {bad}. "
            "MVP only supports row-local expressions."
        )

    for node in expr_obj.walk():
        fn = maybe_function_name(node)
        if fn is not None and fn not in ALLOWED_FUNCTION_NAMES:
            raise UnsupportedCheckConstraint(
                f"Unsupported function in CHECK expression: {fn}. "
                f"Allowed functions: {sorted(ALLOWED_FUNCTION_NAMES)}"
            )


def collect_columns(expr_obj: exp.Expression) -> List[str]:
    cols = []
    seen = set()

    for col in expr_obj.find_all(exp.Column):
        name = col.name
        if name and name not in seen:
            seen.add(name)
            cols.append(name)

    return cols


def find_constraint_name(check_node: exp.Expression, statement: exp.Expression) -> Optional[str]:
    current = check_node
    best_match = None
    best_len = None
    depth = 0

    while current is not None and current is not statement and depth <= 4:
        try:
            chunk_sql = current.sql(dialect="postgres").strip()
        except Exception:
            current = current.parent
            depth += 1
            continue

        match = LOCAL_NAMED_CHECK_RE.match(chunk_sql)
        if match:
            chunk_len = len(chunk_sql)
            if best_len is None or chunk_len < best_len:
                best_len = chunk_len
                best_match = strip_quotes(match.group("name"))

        current = current.parent
        depth += 1

    return best_match


def infer_check_removal_target(check_node: exp.Expression, statement: exp.Expression) -> exp.Expression:
    target = check_node
    best_len = None

    current = check_node
    depth = 0
    while current is not None and current is not statement and depth <= 6:
        try:
            chunk_sql = current.sql(dialect="postgres").strip()
        except Exception:
            current = current.parent
            depth += 1
            continue

        if LOCAL_STANDALONE_CHECK_RE.match(chunk_sql):
            chunk_len = len(chunk_sql)
            if best_len is None or chunk_len < best_len:
                best_len = chunk_len
                target = current

        current = current.parent
        depth += 1

    return target


def cleanup_rewritten_create_table_sql(sql_text: str) -> str:
    text = sql_text

    prev = None
    while prev != text:
        prev = text
        text = DANGLING_NAMED_CONSTRAINT_MIDDLE_RE.sub("", text)
        text = DANGLING_NAMED_CONSTRAINT_END_RE.sub("", text)
        text = DANGLING_NAMED_CONSTRAINT_FIRST_MIDDLE_RE.sub("(", text)
        text = DANGLING_NAMED_CONSTRAINT_FIRST_END_RE.sub("(", text)
        text = re.sub(r",\s*,", ", ", text)
        text = re.sub(r"\(\s*,", "(", text)
        text = re.sub(r",\s*\)", ")", text)
        text = re.sub(r"\s+\)", ")", text)

    return text


def qualify_with_record(expr_obj: exp.Expression, table_base_name: str, record_name: str) -> exp.Expression:
    allowed_table_aliases = {"", table_base_name, strip_quotes(table_base_name), "new", "old"}

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            table = strip_quotes(str(node.table)) if node.table else ""

            if table.lower() in {"new", "old"}:
                return node

            allowed_lower = {t.lower() for t in allowed_table_aliases}
            if table.lower() in allowed_lower:
                return exp.column(node.name, table=record_name)

        return node

    return expr_obj.copy().transform(transform)


def extract_check_specs_from_statement(
    statement: exp.Expression,
    sequence_counters: Dict[str, int],
) -> List[InternalCheckSpec]:
    table_key, table_ref, table_base_name = get_table_info(statement)
    raw_statement_sql = statement.sql(dialect="postgres")
    source_kind = statement.__class__.__name__.lower()

    specs = []
    check_nodes = [node for node in statement.walk() if is_check_node(node)]

    for check_node in check_nodes:
        condition = extract_condition(check_node)
        validate_supported_expr(condition)

        constraint_name = find_constraint_name(check_node, statement)
        synthetic = False

        if not constraint_name:
            sequence_counters[table_key] += 1
            constraint_name = f"ck_{sanitize_identifier(table_base_name)}_{sequence_counters[table_key]}"
            synthetic = True

        specs.append(
            InternalCheckSpec(
                table_key=table_key,
                table_ref=table_ref,
                table_base_name=table_base_name,
                constraint_name=constraint_name,
                condition=condition,
                condition_sql=condition.sql(dialect="postgres"),
                dependent_columns=collect_columns(condition),
                source_statement_sql=raw_statement_sql,
                source_kind=source_kind,
                synthetic_name=synthetic,
            )
        )

    return specs


def rewrite_statement_without_checks(statement: exp.Expression, warnings: List[str]) -> Optional[str]:
    statement_type = statement.__class__.__name__
    statement_copy = statement.copy()

    check_nodes = [node for node in statement_copy.walk() if is_check_node(node)]
    if not check_nodes:
        return statement_copy.sql(dialect="postgres")

    if statement_type == "Alter":
        original_sql = statement.sql(dialect="postgres").strip()
        if original_sql.upper().startswith("ALTER TABLE"):
            return None

        warnings.append(
            "Skipped ALTER statement with CHECK because mixed ALTER forms are not handled safely yet: "
            f"{original_sql}"
        )
        return None

    removal_targets = []
    seen_ids = set()

    for check_node in check_nodes:
        target = infer_check_removal_target(check_node, statement_copy)
        if id(target) not in seen_ids:
            seen_ids.add(id(target))
            removal_targets.append(target)

    for target in removal_targets:
        target.pop()

    rendered = statement_copy.sql(dialect="postgres")
    return cleanup_rewritten_create_table_sql(rendered)


def deduplicate_specs(specs: List[InternalCheckSpec]) -> List[InternalCheckSpec]:
    seen = set()
    out = []

    for spec in specs:
        key = (spec.constraint_name, spec.condition_sql)
        if key not in seen:
            seen.add(key)
            out.append(spec)

    return out


def build_manifest(result: CompileResult) -> Dict:
    tables = {}

    for table_key, specs in result.checks_by_table.items():
        tables[table_key] = []
        for spec in deduplicate_specs(specs):
            manifest_spec = CheckSpec(
                table_key=spec.table_key,
                table_ref=spec.table_ref,
                table_base_name=spec.table_base_name,
                constraint_name=spec.constraint_name,
                condition_sql=spec.condition_sql,
                dependent_columns=spec.dependent_columns,
                source_statement_sql=spec.source_statement_sql,
                source_kind=spec.source_kind,
                synthetic_name=spec.synthetic_name,
            )
            tables[table_key].append(asdict(manifest_spec))

    return {
        "tables": tables,
        "warnings": result.warnings,
    }


def build_table_function_name(table_key: str) -> str:
    return sanitize_identifier(f"trg_check_{table_key}_constraints")


def build_single_constraint_function_name(table_key: str, constraint_name: str) -> str:
    return sanitize_identifier(f"trg_check_{table_key}_{constraint_name}")


def build_insert_trigger_name(table_key: str) -> str:
    return sanitize_identifier(f"check_{table_key}_insert")


def build_update_trigger_name(table_key: str) -> str:
    return sanitize_identifier(f"check_{table_key}_update")


def build_single_insert_trigger_name(table_key: str, constraint_name: str) -> str:
    return sanitize_identifier(f"check_{table_key}_{constraint_name}_insert")


def build_single_update_trigger_name(table_key: str, constraint_name: str) -> str:
    return sanitize_identifier(f"check_{table_key}_{constraint_name}_update")


def compile_table_function_sql(table_key: str, specs: List[InternalCheckSpec]) -> str:
    specs = deduplicate_specs(specs)
    function_name = build_table_function_name(table_key)
    table_name_for_msg = specs[0].table_key if specs else table_key

    lines = [
        f"CREATE OR REPLACE FUNCTION {function_name}()",
        "RETURNS trigger AS $$",
        "BEGIN",
    ]

    for spec in specs:
        qualified = qualify_with_record(spec.condition, spec.table_base_name, "NEW")
        qualified_sql = qualified.sql(dialect="postgres")
        constraint_name_sql = sql_literal(spec.constraint_name)
        msg_sql = sql_literal(
            f'check constraint "{spec.constraint_name}" violated on table "{table_name_for_msg}": {spec.condition_sql}'
        )

        lines.append(f"    -- constraint: {spec.constraint_name}")
        lines.append(f"    IF ({qualified_sql}) IS FALSE THEN")
        lines.append(f"        RAISE EXCEPTION {msg_sql}")
        lines.append("            USING ERRCODE = '23514',")
        lines.append(f"                  CONSTRAINT = {constraint_name_sql};")
        lines.append("    END IF;")
        lines.append("")

    lines.extend([
        "    RETURN NEW;",
        "END;",
        "$$ LANGUAGE plpgsql;",
    ])

    return "\n".join(lines)


def compile_single_trigger_sql_vA(table_key: str, table_ref: str) -> str:
    function_name = build_table_function_name(table_key)
    trigger_name = sanitize_identifier(f"check_{table_key}_all")

    return (
        f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_ref};\n"
        f"CREATE TRIGGER {trigger_name}\n"
        f"BEFORE INSERT OR UPDATE ON {table_ref}\n"
        f"FOR EACH ROW\n"
        f"EXECUTE FUNCTION {function_name}();"
    )


def compile_insert_trigger_sql_vB(table_key: str, table_ref: str) -> str:
    function_name = build_table_function_name(table_key)
    trigger_name = build_insert_trigger_name(table_key)

    return (
        f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_ref};\n"
        f"CREATE TRIGGER {trigger_name}\n"
        f"BEFORE INSERT ON {table_ref}\n"
        f"FOR EACH ROW\n"
        f"EXECUTE FUNCTION {function_name}();"
    )


def compile_update_trigger_sql_vB(table_key: str, table_ref: str, specs: List[InternalCheckSpec]) -> str:
    function_name = build_table_function_name(table_key)
    trigger_name = build_update_trigger_name(table_key)

    all_columns = []
    seen = set()

    for spec in specs:
        for col in spec.dependent_columns:
            if col not in seen:
                seen.add(col)
                all_columns.append(col)

    if all_columns:
        update_of = ", ".join(render_identifier(col) for col in all_columns)
        when_pred = " OR ".join(
            f"{render_qualified_identifier('OLD', col)} IS DISTINCT FROM {render_qualified_identifier('NEW', col)}"
            for col in all_columns
        )

        return (
            f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_ref};\n"
            f"CREATE TRIGGER {trigger_name}\n"
            f"BEFORE UPDATE OF {update_of} ON {table_ref}\n"
            f"FOR EACH ROW\n"
            f"WHEN ({when_pred})\n"
            f"EXECUTE FUNCTION {function_name}();"
        )

    return (
        f"DROP TRIGGER IF EXISTS {trigger_name} ON {table_ref};\n"
        f"CREATE TRIGGER {trigger_name}\n"
        f"BEFORE UPDATE ON {table_ref}\n"
        f"FOR EACH ROW\n"
        f"EXECUTE FUNCTION {function_name}();"
    )


def compile_single_constraint_function_sql(spec: InternalCheckSpec) -> str:
    function_name = build_single_constraint_function_name(spec.table_key, spec.constraint_name)
    table_name_for_msg = spec.table_key

    qualified = qualify_with_record(spec.condition, spec.table_base_name, "NEW")
    qualified_sql = qualified.sql(dialect="postgres")
    constraint_name_sql = sql_literal(spec.constraint_name)
    msg_sql = sql_literal(
        f'check constraint "{spec.constraint_name}" violated on table "{table_name_for_msg}": {spec.condition_sql}'
    )

    lines = [
        f"CREATE OR REPLACE FUNCTION {function_name}()",
        "RETURNS trigger AS $$",
        "BEGIN",
        f"    IF ({qualified_sql}) IS FALSE THEN",
        f"        RAISE EXCEPTION {msg_sql}",
        "            USING ERRCODE = '23514',",
        f"                  CONSTRAINT = {constraint_name_sql};",
        "    END IF;",
        "",
        "    RETURN NEW;",
        "END;",
        "$$ LANGUAGE plpgsql;",
    ]

    return "\n".join(lines)


def compile_single_constraint_insert_trigger_sql(spec: InternalCheckSpec) -> str:
    function_name = build_single_constraint_function_name(spec.table_key, spec.constraint_name)
    trigger_name = build_single_insert_trigger_name(spec.table_key, spec.constraint_name)

    return (
        f"DROP TRIGGER IF EXISTS {trigger_name} ON {spec.table_ref};\n"
        f"CREATE TRIGGER {trigger_name}\n"
        f"BEFORE INSERT ON {spec.table_ref}\n"
        f"FOR EACH ROW\n"
        f"EXECUTE FUNCTION {function_name}();"
    )


def compile_single_constraint_update_trigger_sql(spec: InternalCheckSpec) -> str:
    function_name = build_single_constraint_function_name(spec.table_key, spec.constraint_name)
    trigger_name = build_single_update_trigger_name(spec.table_key, spec.constraint_name)

    cols = spec.dependent_columns[:]

    if cols:
        update_of = ", ".join(render_identifier(col) for col in cols)
        when_pred = " OR ".join(
            f"{render_qualified_identifier('OLD', col)} IS DISTINCT FROM {render_qualified_identifier('NEW', col)}"
            for col in cols
        )
        return (
            f"DROP TRIGGER IF EXISTS {trigger_name} ON {spec.table_ref};\n"
            f"CREATE TRIGGER {trigger_name}\n"
            f"BEFORE UPDATE OF {update_of} ON {spec.table_ref}\n"
            f"FOR EACH ROW\n"
            f"WHEN ({when_pred})\n"
            f"EXECUTE FUNCTION {function_name}();"
        )

    return (
        f"DROP TRIGGER IF EXISTS {trigger_name} ON {spec.table_ref};\n"
        f"CREATE TRIGGER {trigger_name}\n"
        f"BEFORE UPDATE ON {spec.table_ref}\n"
        f"FOR EACH ROW\n"
        f"EXECUTE FUNCTION {function_name}();"
    )


def compile_bplus_bundle(specs: List[InternalCheckSpec]) -> str:
    specs = deduplicate_specs(specs)
    chunks = []

    for spec in specs:
        chunks.append(compile_single_constraint_function_sql(spec))
        chunks.append("")
        chunks.append(compile_single_constraint_insert_trigger_sql(spec))
        chunks.append("")
        chunks.append(compile_single_constraint_update_trigger_sql(spec))
        chunks.append("")

    return "\n".join(chunks).strip()


def compile_triggers_for_table(specs: List[InternalCheckSpec], level: str = "B") -> str:
    specs = deduplicate_specs(specs)
    if not specs:
        return ""

    table_key = specs[0].table_key
    table_ref = specs[0].table_ref
    upper_level = level.upper()

    if upper_level == "A":
        return "\n".join([
            compile_table_function_sql(table_key, specs),
            "",
            compile_single_trigger_sql_vA(table_key, table_ref),
        ])

    if upper_level == "B":
        return "\n".join([
            compile_table_function_sql(table_key, specs),
            "",
            compile_insert_trigger_sql_vB(table_key, table_ref),
            "",
            compile_update_trigger_sql_vB(table_key, table_ref, specs),
        ])

    if upper_level == "BPLUS":
        return compile_bplus_bundle(specs)

    raise ValueError(f"Unknown level: {level}")


def compile_check_constraints(sql: str) -> CompileResult:
    parsed = parse_sql(sql)
    result = CompileResult()
    sequence_counters = defaultdict(int)

    for statement in parsed:
        try:
            specs = extract_check_specs_from_statement(statement, sequence_counters)

            for spec in specs:
                result.checks_by_table[spec.table_key].append(spec)

            rewritten = rewrite_statement_without_checks(statement, result.warnings)
            if rewritten and rewritten.strip():
                result.modified_statements.append(rewritten)

        except UnsupportedCheckConstraint as exc:
            result.warnings.append(
                f"Skipped statement because of unsupported CHECK expression: {exc}\n"
                f"Statement: {statement.sql(dialect='postgres')}"
            )
            result.modified_statements.append(statement.sql(dialect="postgres"))

    return result


def emit_outputs(result: CompileResult, level: str = "B") -> Tuple[str, str, str]:
    rewritten_sql = ""
    if result.modified_statements:
        rewritten_sql = ";\n\n".join(stmt.rstrip(";") for stmt in result.modified_statements) + ";\n"

    trigger_blocks = []
    for specs in result.checks_by_table.values():
        block = compile_triggers_for_table(specs, level=level)
        if block.strip():
            trigger_blocks.append(block)

    triggers_sql = "\n\n".join(trigger_blocks)
    if triggers_sql:
        triggers_sql += "\n"

    manifest_json = json.dumps(build_manifest(result), indent=2, ensure_ascii=False)
    return rewritten_sql, triggers_sql, manifest_json


def get_demo_sql() -> str:
    return """
    CREATE TABLE employees (
        id INT PRIMARY KEY,
        salary INT CHECK (salary > 0),
        bonus INT,
        CONSTRAINT ck_bonus CHECK (bonus >= 0 AND bonus <= salary),
        name TEXT,
        CHECK (id > 0)
    );

    ALTER TABLE employees
    ADD CONSTRAINT ck_name_len CHECK (length(name) <= 50);
    """


def add_level_suffix(path_str: str, level: str) -> str:
    path = Path(path_str)
    return str(path.with_name(f"{path.stem}_{level}{path.suffix}"))


def resolve_output_path(path_str: str, input_file: Optional[Path]) -> Path:
    path = Path(path_str)
    if path.is_absolute() or input_file is None:
        return path
    return input_file.parent / path


def write_outputs_for_level(
    result: CompileResult,
    level: str,
    rewritten_out: Path,
    triggers_out: Path,
    manifest_out: Path,
) -> Tuple[str, str, str]:
    rewritten_sql, triggers_sql, manifest_json = emit_outputs(result, level=level)

    rewritten_out.parent.mkdir(parents=True, exist_ok=True)
    triggers_out.parent.mkdir(parents=True, exist_ok=True)
    manifest_out.parent.mkdir(parents=True, exist_ok=True)

    rewritten_out.write_text(rewritten_sql, encoding="utf-8")
    triggers_out.write_text(triggers_sql, encoding="utf-8")
    manifest_out.write_text(manifest_json, encoding="utf-8")

    return rewritten_sql, triggers_sql, manifest_json


def build_compile_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile PostgreSQL CHECK constraints into triggers.")
    parser.add_argument("--input", default="create_table.sql", help="Input SQL file")
    parser.add_argument("--rewritten-out", default="out/rewritten.sql", help="Base output rewritten DDL")
    parser.add_argument("--triggers-out", default="out/compiled_triggers.sql", help="Base output generated trigger SQL")
    parser.add_argument("--manifest-out", default="out/manifest.json", help="Base output manifest JSON")
    parser.add_argument(
        "--level",
        choices=[*SUPPORTED_LEVELS, "ALL"],
        default="ALL",
        help="Compilation level",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_compile_parser()
    args = parser.parse_args(argv)

    input_file = Path(args.input)

    if input_file.exists():
        sql = input_file.read_text(encoding="utf-8")
        resolved_input_file = input_file.resolve()
    else:
        print(f"Warning: {args.input} not found. Using built-in demo SQL.\n")
        sql = get_demo_sql()
        resolved_input_file = None

    result = compile_check_constraints(sql)
    levels = list(SUPPORTED_LEVELS) if args.level == "ALL" else [args.level]

    for level in levels:
        if args.level == "ALL":
            rewritten_out = resolve_output_path(add_level_suffix(args.rewritten_out, level), resolved_input_file)
            triggers_out = resolve_output_path(add_level_suffix(args.triggers_out, level), resolved_input_file)
            manifest_out = resolve_output_path(add_level_suffix(args.manifest_out, level), resolved_input_file)
        else:
            rewritten_out = resolve_output_path(args.rewritten_out, resolved_input_file)
            triggers_out = resolve_output_path(args.triggers_out, resolved_input_file)
            manifest_out = resolve_output_path(args.manifest_out, resolved_input_file)

        rewritten_sql, triggers_sql, manifest_json = write_outputs_for_level(
            result=result,
            level=level,
            rewritten_out=rewritten_out,
            triggers_out=triggers_out,
            manifest_out=manifest_out,
        )

        print(f"\n{'=' * 70}")
        print(f"=== Rewritten SQL (Level {level}) ===")
        print(rewritten_sql if rewritten_sql.strip() else "-- no rewritten statements --")

        print(f"\n=== Generated trigger SQL (Level {level}) ===")
        print(triggers_sql if triggers_sql.strip() else "-- no triggers generated --")

        print(f"\n=== Manifest JSON (Level {level}) ===")
        print(manifest_json)

        print(f"\n=== Files written for Level {level} ===")
        print(rewritten_out)
        print(triggers_out)
        print(manifest_out)

    if result.warnings:
        print("\n=== Warnings ===")
        for warning in result.warnings:
            print("-", warning)


if __name__ == "__main__":
    main()
