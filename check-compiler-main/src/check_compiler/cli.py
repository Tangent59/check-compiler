import argparse
from typing import List, Optional

from .artifacts import generate_case_artifacts
from .benchmark import main as benchmark_main
from .compiler import SUPPORTED_LEVELS, main as compile_main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compiler and benchmark utilities for CHECK-to-trigger experiments.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser("compile", help="Compile CHECK constraints into trigger SQL.")
    compile_parser.add_argument("--input", default="create_table.sql", help="Input SQL file")
    compile_parser.add_argument("--rewritten-out", default="out/rewritten.sql", help="Base output rewritten DDL")
    compile_parser.add_argument("--triggers-out", default="out/compiled_triggers.sql", help="Base output generated trigger SQL")
    compile_parser.add_argument("--manifest-out", default="out/manifest.json", help="Base output manifest JSON")
    compile_parser.add_argument(
        "--level",
        choices=[*SUPPORTED_LEVELS, "ALL"],
        default="ALL",
        help="Compilation level",
    )

    case_parser = subparsers.add_parser("generate-case", help="Generate benchmark/test SQL artifacts from a case spec.")
    case_parser.add_argument("--case-spec", required=True, help="Path to the JSON case specification.")
    case_parser.add_argument("--out-dir", default="artifacts/cases", help="Destination directory for generated artifacts.")

    benchmark_parser = subparsers.add_parser("benchmark", help="Run the benchmark using generated SQL artifacts.")
    benchmark_parser.add_argument("--case-dir", required=True, help="Artifact case directory produced by generate-case.")
    benchmark_parser.add_argument("--row-count", type=int, default=20000, help="Rows inserted for each benchmark run.")
    benchmark_parser.add_argument("--repeats", type=int, default=5, help="Measured runs per workload.")
    benchmark_parser.add_argument("--warmup", type=int, default=1, help="Warm-up runs per workload.")
    benchmark_parser.add_argument("--variants", nargs="*", default=None, help="Subset of variants to run.")
    benchmark_parser.add_argument("--json-out", help="Optional path for JSON benchmark output.")

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "compile":
        compile_main([
            "--input",
            args.input,
            "--rewritten-out",
            args.rewritten_out,
            "--triggers-out",
            args.triggers_out,
            "--manifest-out",
            args.manifest_out,
            "--level",
            args.level,
        ])
        return

    if args.command == "generate-case":
        case_dir = generate_case_artifacts(args.case_spec, args.out_dir)
        print(case_dir)
        return

    if args.command == "benchmark":
        benchmark_args: List[str] = [
            "--case-dir",
            args.case_dir,
            "--row-count",
            str(args.row_count),
            "--repeats",
            str(args.repeats),
            "--warmup",
            str(args.warmup),
        ]
        if args.variants:
            benchmark_args.extend(["--variants", *args.variants])
        if args.json_out:
            benchmark_args.extend(["--json-out", args.json_out])
        benchmark_main(benchmark_args)
        return

    parser.error(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
