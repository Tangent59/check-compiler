import json

from check_compiler.artifacts import generate_case_artifacts, load_case_metadata


def test_generate_case_artifacts(tmp_path):
    case_dir = generate_case_artifacts("examples/employees_case.json", tmp_path)

    metadata = load_case_metadata(case_dir)
    assert metadata["case_name"] == "employees"
    assert set(metadata["variants"]) == {"native", "A", "B", "BPLUS"}

    native_setup = (case_dir / "native" / "setup.sql").read_text(encoding="utf-8")
    assert "CREATE SCHEMA bench_employees_native;" in native_setup
    assert "CREATE TABLE bench_employees_native.employees" in native_setup

    rewritten_a = (case_dir / "a" / "rewritten.sql").read_text(encoding="utf-8")
    assert "CHECK" not in rewritten_a
    assert "bench_employees_a.employees" in rewritten_a

    triggers_b = (case_dir / "b" / "triggers.sql").read_text(encoding="utf-8")
    assert "BEFORE UPDATE OF id, bonus, salary, name" in triggers_b
    assert "bench_employees_b.employees" in triggers_b

    manifest_bplus = json.loads((case_dir / "bplus" / "manifest.json").read_text(encoding="utf-8"))
    assert "bench_employees_bplus.employees" in manifest_bplus["tables"]
