from pathlib import Path

from check_compiler.compiler import add_level_suffix, resolve_output_path


def test_resolve_output_path_is_relative_to_input_file():
    input_file = Path("/tmp/project/examples/employees.sql")
    resolved = resolve_output_path("out/rewritten.sql", input_file)
    assert resolved == Path("/tmp/project/examples/out/rewritten.sql")


def test_add_level_suffix():
    assert add_level_suffix("out/rewritten.sql", "B") == "out/rewritten_B.sql"
