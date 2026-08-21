# Legacy Notes

This file preserves notes that originally lived in the old `cs5421 project/` subfolder.

## Existing compiler variants

The three implemented variants share the same core pipeline:

- parse input SQL
- extract supported `CHECK` constraints
- remove those `CHECK` constraints from the table definition
- generate equivalent PostgreSQL trigger functions and triggers
- enforce the constraints during `INSERT` and `UPDATE`

Their main difference is optimization granularity:

- Version A: one shared trigger function per table
- Version B: one shared trigger function per table, with table-level dependency filtering
- Version B+: one trigger function per constraint

## Summary of observed benchmark behavior

### Version A

Version A is the baseline implementation. All constraints on the same table are compiled into a single shared trigger function, and the table gets one `BEFORE INSERT OR UPDATE` row trigger. Every write that reaches the trigger checks every compiled constraint.

This is the simplest and most direct version. It proves the compiler pipeline is viable, but it is also the least selective because unrelated updates still cause all constraints to be re-checked.

### Version B

Version B keeps the shared-function design, but adds table-level dependency analysis. It splits the trigger into separate `INSERT` and `UPDATE` triggers, uses `UPDATE OF ...` for dependent columns, and adds `WHEN (OLD.col IS DISTINCT FROM NEW.col)` filters.

This reduces unnecessary trigger execution for unrelated updates, although once the function runs it still checks all constraints on the table.

### Version B+

Version B+ compiles each `CHECK` constraint separately. Every constraint receives its own function and its own `INSERT` and `UPDATE` triggers. Each `UPDATE` trigger listens only to columns required by that specific constraint.

This gives the finest granularity, but may introduce more trigger dispatch overhead.

## Legacy benchmark summary

### INSERT

- `employees_native`: avg `0.7335s`, median `0.7314s`
- `employees_a`: avg `0.7396s`, median `0.7387s`
- `employees_b`: avg `0.9367s`, median `0.7458s`
- `employees_bplus`: avg `1.3627s`, median `1.3735s`

Interpretation:

- `INSERT` usually requires checking all constraints anyway.
- B and B+ gain little from dependency filtering here.
- B+ is the slowest because it pays more trigger dispatch cost.

### UPDATE of a related column: `salary`

- `employees_native`: avg `0.0372s`, median `0.0363s`
- `employees_a`: avg `0.0690s`, median `0.0571s`
- `employees_b`: avg `0.0646s`, median `0.0565s`
- `employees_bplus`: avg `0.0871s`, median `0.0833s`

Interpretation:

- A always checks all constraints.
- B can be slightly better than A because it avoids some irrelevant work.
- B+ does not always win, because finer granularity can still lose to dispatch overhead.

### UPDATE of another related column: `name`

- `employees_native`: avg `0.0578s`, median `0.0413s`
- `employees_a`: avg `0.0616s`, median `0.0528s`
- `employees_b`: avg `0.0568s`, median `0.0558s`
- `employees_bplus`: avg `0.0616s`, median `0.0570s`

### UPDATE of an unrelated column: `misc`

- `employees_native`: avg `0.0423s`, median `0.0384s`
- `employees_a`: avg `0.0542s`, median `0.0539s`
- `employees_b`: avg `0.0431s`, median `0.0418s`
- `employees_bplus`: avg `0.0423s`, median `0.0415s`

Interpretation:

- This is the strongest result for dependency analysis.
- A still fires and performs redundant checks.
- B and B+ can skip almost all unnecessary work for unrelated updates.

## Remaining gaps noted in the legacy notes

- The experiments still need systematic `BEFORE` vs `AFTER` comparison.
- The experiments still need systematic `FOR EACH ROW` vs `FOR EACH STATEMENT` comparison.
- The benchmark needs much stronger statistical rigor, including automated larger run counts, warm-up handling, and more detailed reporting.
