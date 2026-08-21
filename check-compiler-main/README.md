# CHECK-to-Trigger Compiler for PostgreSQL

This project explores a small compiler that rewrites PostgreSQL `CHECK (...)` constraints into equivalent `trigger function + trigger` implementations.

The goal is not only to prove that `CHECK` constraints can be compiled into triggers, but also to compare the performance of different compilation strategies under different workloads.

## 1. Strategy Overview: Version A / B / B+

You can think of each `CHECK` constraint as a guard rule. For example, suppose a table has three rules:

- `age > 18`
- `salary > 0`
- `char_length(name) >= 2`

For every `INSERT` or `UPDATE`, the system must decide:

- Which rules need to be checked?
- How should they be checked?
- How many times should they be checked?

### Version A: one shared guard for all rules

Version A is the simplest design:

- Generate one shared trigger function for the whole table.
- Generate one trigger for the whole table.
- Every inserted or updated row enters the same function.
- The function checks every compiled constraint on that table.

In other words, even if an update only modifies `misc`, the trigger still re-checks all constraints.

Characteristics of Version A:

- Pros: simplest implementation, easiest way to prove the compiler pipeline works.
- Cons: coarse-grained, performs many unnecessary checks.

### Version B: still one shared guard, but only trigger on relevant columns

Version B keeps the same high-level structure as Version A, but optimizes trigger activation.

It analyzes which columns each `CHECK` depends on. For example:

- `salary > 0` depends on `salary`
- `char_length(name) >= 2` depends on `name`

Then it combines all referenced columns and uses them to optimize `UPDATE` triggers.

Version B behaves like this:

- `INSERT`: still checks constraints, because all relevant conditions may matter for a new row.
- `UPDATE`: if only unrelated columns are modified, the trigger does not fire at all.
- Even when it does fire, it uses `WHEN (OLD.col IS DISTINCT FROM NEW.col)` to avoid entering the function unless relevant column values actually changed.

So Version B optimizes at the table level:

- It reduces unnecessary trigger executions.
- But once the shared function runs, it still checks all constraints on that table.

### Version B+: one dedicated guard per constraint

Version B+ goes one step further and compiles each `CHECK` independently:

- Generate one trigger function per `CHECK`.
- Generate one trigger per `CHECK`.
- Each trigger listens only to the columns required by that specific constraint.

For example:

- `salary > 0` gets its own trigger/function pair.
- `char_length(name) >= 2` gets another trigger/function pair.

As a result:

- Updating `salary` only checks the salary-related rule.
- Updating `name` only checks the name-related rule.
- Updating `misc` should not activate any unrelated compiled checks.

Characteristics of Version B+:

- Pros: finest granularity, most compiler-like optimization.
- Cons: more trigger/function objects, higher dispatch overhead.

This means B+ is not guaranteed to be faster in all workloads. It is simply more selective.

### Granularity comparison

- Version A: every write checks every constraint on the table.
- Version B: only relevant updates trigger checking, but the shared function still checks every constraint.
- Version B+: only relevant constraints are checked.

Optimization granularity:

`A < B < B+`

## 2. Experimental Results

### 2.1 INSERT: A is closest to native, B+ is the slowest

- native: `0.7335s`
- A: `0.7396s`
- B: `0.9367s`
- B+: `1.3627s`

Why:

- For `INSERT`, a new row must usually satisfy all constraints anyway.
- The column-based filtering of B and B+ brings little benefit.
- B+ pays extra dispatch cost because it has more triggers.

Observation:

- B+ can become over-optimized for `INSERT`.
- The `employees_b` average is skewed by two high runs, so the median is more informative than the mean for that case.

### 2.2 UPDATE of a relevant column: B may beat A, but B+ does not always win

For example, updating `salary`:

- A: `0.0690`
- B: `0.0646`
- B+: `0.0871`

Why:

- A always checks every constraint.
- B avoids irrelevant updates, then runs the shared function.
- B+ checks fewer constraints, but extra trigger dispatch starts to matter.

Conclusion:

- Finer granularity does not automatically mean better performance.
- When the number of constraints is small, B may be the better trade-off.

### 2.3 UPDATE of an unrelated column: B and B+ show the clearest benefit

When updating `misc`:

- native: `0.0423`
- A: `0.0542`
- B: `0.0431`
- B+: `0.0423`

Why:

- `misc` is unrelated to the compiled checks.
- A still fires and performs redundant work.
- B and B+ can skip almost all unnecessary checking.

This is one of the main results the project is trying to demonstrate:

- dependency analysis is valuable.

## 3. Compiler Pipeline

### 3.1 Read input SQL

Example input:

```sql
CREATE TABLE employees (
    id INT,
    age INT,
    salary INT,
    name TEXT,
    CHECK (age > 18),
    CHECK (salary > 0),
    CHECK (char_length(name) >= 2)
);
```

### 3.2 Parse SQL and extract CHECK constraints

The compiler needs to identify:

- table name
- column definitions
- `CHECK` constraints
- constraint names, if explicitly provided
- the expression of each `CHECK`

Using an open-source SQL parser for parsing is acceptable. The core compilation logic must still be implemented manually.

### 3.3 Analyze dependent columns

Examples:

- `age > 18` depends on `age`
- `salary > 0` depends on `salary`
- `char_length(name) >= 2` depends on `name`

This dependency analysis is the foundation of Version B and Version B+.

### 3.4 Rewrite the original DDL

The compiler removes `CHECK` constraints from the original table definition and produces a rewritten table without native checks.

This is necessary because the project aims to show that native `CHECK` semantics can be replaced by generated trigger-based enforcement.

### 3.5 Generate equivalent trigger functions and triggers

Depending on Version A / B / B+ / C, the compiler generates different SQL for:

- function names
- trigger names
- `BEFORE` vs `AFTER`
- `FOR EACH ROW` vs `FOR EACH STATEMENT`
- `UPDATE OF` column lists
- `WHEN` conditions

### 3.6 Preserve semantics as closely as possible

According to PostgreSQL semantics:

- `CHECK` passes when the expression evaluates to `TRUE` or `UNKNOWN` (`NULL`).
- It fails only when the expression evaluates to `FALSE`.
- `CHECK` is intended for row-local conditions, not cross-row or cross-table logic.

So the compiler must be careful about:

- preserving `NULL` behavior
- clearly limiting the supported scope to row-local `CHECK` expressions

Otherwise the translation may compile successfully but still be semantically incorrect.

### 3.7 Validate correctness

Performance alone is not enough. The project must also verify that the behavior matches native `CHECK`.

For the same `INSERT` or `UPDATE` workload:

- run once on native `CHECK`
- run once on A / B / B+ / C
- compare whether they all succeed, fail, or raise errors consistently

This is the correctness baseline.

### 3.8 Benchmark performance

Only after correctness is established should performance benchmarking be treated as meaningful.

## 4. Mapping to the Instructor's Five Requirements

### 4.1 Architecture for handling multiple constraints

Question:

> If a table has many `CHECK` constraints, how should they be compiled?
> Should they be merged into one trigger function?
> Or split into separate trigger functions and triggers?

Current progress:

- Version A: all `CHECK` constraints on a table are compiled into one shared function.
- Version B: still one shared function, but with dependency-based trigger filtering.
- Version B+: each `CHECK` is compiled separately, giving constraint-level triggering and checking.

What should be improved in the report:

- explain the architectural difference between A, B, and B+ more explicitly
- explain why a shared-function design may be better in some workloads
- explain why per-constraint triggers may be better in other workloads

### 4.2 Automatic naming rules

Question:

> If this is a compiler, all generated function names and trigger names must be created automatically.
> They should be deterministic, reproducible, and collision-resistant.

Current progress:

- the script generates trigger function names and trigger names automatically
- shared function names look like `trg_check_employees_constraints`
- per-constraint function names look like `trg_check_employees_ck_bonus`
- unnamed constraints receive synthetic names such as `ck_employees_1`, `ck_employees_2`
- identifiers are sanitized to avoid invalid PostgreSQL object names

What should be documented more clearly:

- how unnamed constraints are named
- how named constraints are preserved
- how function names differ from trigger names
- how collisions across tables are prevented

### 4.3 Experimental control groups

Question:

> The project should not stop at proving the compiler runs.
> It should compare multiple trigger designs experimentally.

Current progress:

- there is already a good first comparison group: native `CHECK` vs A vs B vs B+
- workloads already include `INSERT`, relevant-column `UPDATE`, name-column `UPDATE`, and unrelated-column `UPDATE`

What is still missing:

- the comparison dimensions explicitly requested by the instructor:
  - `BEFORE` vs `AFTER`
  - `FOR EACH ROW` vs `FOR EACH STATEMENT`

At the moment, the experiments mostly compare different organization strategies within row-level `BEFORE` triggers. They do not yet systematically compare trigger timing and trigger granularity themselves.

That is one reason Version C matters.

### 4.4 Performance evaluation rigor

Question:

> A few runs are not enough.
> The benchmark should be automated and statistically meaningful.
> The requirement is at least 1000 runs for averaging.

Current progress:

- each workload has been run 5 times
- the report already records average, median, and per-run values

What should be improved:

- full automation
- at least 1000 runs per group
- fixed dataset size
- warm-up runs separated from measured runs
- report `avg`, `median`, and `std`
- optionally generate plots

At the current stage, 5 runs are enough to show rough trends, but not enough to satisfy the instructor's requirement.

### 4.5 Proper use of open-source libraries

Question:

> Open-source libraries may be used for non-core tasks such as SQL parsing.
> But the project's core contribution must remain your own logic.

That means the following should be implemented by the project itself:

- extracting `CHECK` constraints
- analyzing dependent columns
- generating triggers
- implementing optimization strategies

## 5. Main Remaining Gaps

There are still two major gaps:

1. The experimental comparison is not complete.
   The instructor specifically requested `BEFORE vs AFTER` and `ROW vs STATEMENT`, but these dimensions are not yet covered systematically.

2. The benchmark is not rigorous enough.
   Five runs are enough for early exploration, but far from the requirement of at least 1000 automated runs.

## 6. Direction for Version C

A strong definition of Version C would be:

- `AFTER STATEMENT` batch checking using transition tables

In other words:

- one shared function per table, or possibly one per constraint
- but use statement-level triggers such as:
  - `AFTER INSERT ... FOR EACH STATEMENT`
  - `AFTER UPDATE ... FOR EACH STATEMENT`
- use transition tables to access all rows affected by the statement
- inside the function, execute a query that checks whether any affected row violates the compiled predicate
- raise an exception if any violation is found

Example:

```sql
UPDATE employees
SET salary = salary + 100
WHERE dept = 'sales';
```

If this updates 500 rows:

- A / B / B+ are fundamentally row-triggered and check rows one by one
- C would fire once after the statement and validate the entire affected row set in batch

Potential strengths of Version C:

- only one trigger execution per statement
- likely lower dispatch overhead for large batch updates

Potential weaknesses of Version C:

- may not filter unrelated-column updates as elegantly as B or B+
- more complex implementation
- requires SQL generation for transition-table-based validation

## 7. Summary

The current work already establishes a meaningful progression:

- Version A proves the basic compilation path works.
- Version B shows that table-level dependency analysis can reduce unnecessary trigger execution.
- Version B+ explores finer-grained constraint-level compilation.

The next stage should focus on:

- implementing Version C
- building a correctness test framework against native `CHECK`
- expanding the benchmark into a more rigorous automated evaluation
- adding the missing `BEFORE/AFTER` and `ROW/STATEMENT` comparison axes

## 8. Implemented Artifact-Driven Workflow

The repository now includes an artifact-driven workflow for benchmarks and tests:

1. Write or update a SQL fixture.
2. Generate benchmark artifacts from that fixture.
3. Let the benchmark runner and test suite read the generated SQL files.
4. Execute workloads against native and compiled variants.

### Case specification

The example benchmark case is defined in:

- `examples/employees.sql`
- `examples/employees_case.json`

The JSON case specification defines:

- the input SQL fixture
- the logical table name
- insert columns
- the row dataset preset
- workload SQL templates

### Generated artifact layout

Running case generation produces a directory like:

```text
artifacts/cases/employees/
  case.json
  case_spec.json
  source.sql
  native/setup.sql
  a/setup.sql
  a/rewritten.sql
  a/triggers.sql
  a/manifest.json
  b/setup.sql
  b/rewritten.sql
  b/triggers.sql
  b/manifest.json
  bplus/setup.sql
  bplus/rewritten.sql
  bplus/triggers.sql
  bplus/manifest.json
```

The benchmark runner reads `setup.sql` from each variant instead of embedding table DDL in Python code.

### Commands

Generate benchmark artifacts:

```bash
PYTHONPATH=src python3 -m check_compiler generate-case \
  --case-spec examples/employees_case.json \
  --out-dir artifacts/cases
```

Compile a SQL file directly:

```bash
PYTHONPATH=src python3 -m check_compiler compile \
  --input examples/employees_with_alter.sql \
  --level B
```

Run the benchmark from generated artifacts:

```bash
PYTHONPATH=src python3 -m check_compiler benchmark \
  --case-dir artifacts/cases/employees \
  --row-count 20000 \
  --repeats 5 \
  --warmup 1
```

Run unit tests:

```bash
pytest -q
```

### Dependency note

Unit tests only require the Python compiler dependencies.

PostgreSQL benchmarks require `psycopg`, for example:

```bash
pip install ".[postgres]"
```
