import argparse
import json
import os
import statistics
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .artifacts import load_case_metadata


def get_psycopg():
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for database benchmarks. Install it with "
            "`pip install '.[postgres]'` or `pip install psycopg[binary]`."
        ) from exc
    return psycopg


def connect_kwargs_from_env() -> Dict[str, object]:
    kwargs: Dict[str, object] = {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": os.getenv("PGDATABASE", "postgres"),
        "user": os.getenv("PGUSER", "postgres"),
    }
    password = os.getenv("PGPASSWORD")
    if password:
        kwargs["password"] = password
    return kwargs


def load_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_case_components(case_dir: Path | str) -> Tuple[Dict, Dict[str, str]]:
    resolved_dir = Path(case_dir)
    metadata = load_case_metadata(resolved_dir)
    sql_by_variant = {}

    for label, variant in metadata["variants"].items():
        sql_by_variant[label] = load_sql(resolved_dir / variant["setup_sql"])

    return metadata, sql_by_variant


def make_employees_rows(row_count: int) -> List[Tuple[object, ...]]:
    return [
        (i, 1000 + i, i % 200, f"name_{i}", f"misc_{i}")
        for i in range(1, row_count + 1)
    ]


def build_rows(dataset: str, row_count: int) -> List[Tuple[object, ...]]:
    if dataset == "employees_basic":
        return make_employees_rows(row_count)
    raise ValueError(f"Unsupported dataset preset: {dataset}")


def render_table_ref(schema_name: str, table_name: str) -> str:
    return f"{schema_name}.{table_name}"


def execute_sql(conn, sql: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def setup_variant(conn, setup_sql: str) -> None:
    execute_sql(conn, setup_sql)


def reset_table(conn, table_ref: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {table_ref} CASCADE;")
    conn.commit()


def insert_rows(conn, table_ref: str, insert_columns: Sequence[str], rows: Sequence[Tuple[object, ...]]) -> float:
    start = time.perf_counter()
    columns_sql = ", ".join(insert_columns)
    placeholders = ", ".join(["%s"] * len(insert_columns))
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {table_ref} ({columns_sql}) VALUES ({placeholders})",
            rows,
        )
    conn.commit()
    return time.perf_counter() - start


def execute_workload_sql(conn, table_ref: str, sql_template: str) -> float:
    start = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(sql_template.format(table=table_ref))
    conn.commit()
    return time.perf_counter() - start


def summarize(label: str, timings: Iterable[float]) -> Dict[str, object]:
    values = list(timings)
    summary = {
        "avg": statistics.mean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "runs": [round(value, 4) for value in values],
    }
    if len(values) > 1:
        summary["stdev"] = statistics.stdev(values)
    else:
        summary["stdev"] = 0.0

    print(
        f"{label:<12} avg={summary['avg']:.4f}s  median={summary['median']:.4f}s  "
        f"stdev={summary['stdev']:.4f}s  runs={summary['runs']}"
    )
    return summary


def run_benchmark(
    case_dir: Path | str,
    row_count: int,
    repeats: int,
    warmup: int,
    variants: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, object]]:
    psycopg = get_psycopg()
    metadata, sql_by_variant = load_case_components(case_dir)
    selected_variants = list(variants or metadata["variants"].keys())
    rows = build_rows(metadata["dataset"], row_count)
    results: Dict[str, Dict[str, object]] = {}

    with psycopg.connect(**connect_kwargs_from_env()) as conn:
        for label in selected_variants:
            setup_variant(conn, sql_by_variant[label])

        print(f"Rows per test: {row_count}, repeats: {repeats}, warmup: {warmup}\n")

        for workload in metadata["workloads"]:
            workload_name = workload["name"]
            print(f"=== {workload_name} ===")
            results[workload_name] = {}

            for label in selected_variants:
                variant = metadata["variants"][label]
                table_ref = render_table_ref(variant["schema"], metadata["table_name"])
                timings = []

                for iteration in range(warmup + repeats):
                    reset_table(conn, table_ref)

                    if workload["kind"] == "insert":
                        elapsed = insert_rows(conn, table_ref, metadata["insert_columns"], rows)
                    else:
                        insert_rows(conn, table_ref, metadata["insert_columns"], rows)
                        elapsed = execute_workload_sql(conn, table_ref, workload["sql"])

                    if iteration >= warmup:
                        timings.append(elapsed)

                results[workload_name][label] = summarize(label, timings)
            print("")

    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run benchmarks using generated SQL artifacts.")
    parser.add_argument("--case-dir", required=True, help="Artifact case directory produced by generate-case")
    parser.add_argument("--row-count", type=int, default=20000, help="Rows inserted for each benchmark run")
    parser.add_argument("--repeats", type=int, default=5, help="Measured runs per workload")
    parser.add_argument("--warmup", type=int, default=1, help="Warm-up runs per workload")
    parser.add_argument(
        "--variants",
        nargs="*",
        default=None,
        help="Subset of variant labels to run, for example native A B BPLUS",
    )
    parser.add_argument("--json-out", help="Optional path for JSON benchmark output")
    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    results = run_benchmark(
        case_dir=args.case_dir,
        row_count=args.row_count,
        repeats=args.repeats,
        warmup=args.warmup,
        variants=args.variants,
    )

    if args.json_out:
        json_path = Path(args.json_out)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
