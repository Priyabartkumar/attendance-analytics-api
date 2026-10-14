# DECISIONS.md

**1. Indexes.** Six, created at startup. On `employees`: unique `emp_code` (identity; it also settles the
duplicate-create race), `(department, emp_code)` for the filtered list and its sort, `(department, joined_on)` for
headcount. On `attendance_logs`: unique `(emp_code, date)` (natural key, punch-in race, monthly queries),
`(date desc, emp_code asc)` so the list filter and sort need no blocking sort, `(emp_code, punch_in desc)` for the
record a punch-out closes. Rejected a `status` index: five values, not selective.

**2. Punch-in race.** Both requests read the employee, derive the same date, and call `insert_one` — no
check-then-insert. The unique `(emp_code, date)` index lets one insert commit; the other raises `DuplicateKeyError`,
which becomes 409. With 16 parallel requests: one 201, fifteen 409.

**3. Ties.** `$setWindowFields` with `$rank` ranks *before* `limit`, then I keep every row with `rank <= limit`. A
two-way tie at rank 3 with `limit=3` returns four rows and skips rank 4. Tied rows are ordered by `emp_code`.

**4. Headcount.** The pipeline starts from `employees` (`joined_on <=` month end) and `$lookup`s that month's logs.
Someone with no logs gets an empty array and still counts 1; a later joiner never enters it.

**5. For 100x.** Keep a per-employee daily rollup maintained on write, so analytics read a small pre-aggregated
collection instead of rescanning the logs on every request.
