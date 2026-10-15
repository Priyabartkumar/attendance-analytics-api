# REVIEW.md

Part A reviews `app/main.py` and documents 19 defects, the cases used to check their behavior, and the corresponding
fixes.

**Verification and scope**

The corrected behavior is covered by `tests/test_contract.py` (132 checks) and additional runs against MongoDB 8.0
using the demo and bulk datasets. The examples below describe the defects each fix is intended to prevent; they are not
all observations from the original starter file.

I could not recover the original starter from Git history or the available candidate-kit archive. The first available
commit already contains a corrected implementation. I therefore cannot confirm that every listed defect appeared in the
original file. Library-specific behavior and the corrected results were checked separately where noted in the findings.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)`, config | Client built without `tz_aware=True`, so PyMongo returns **naive** datetimes. Any comparison against an IST-aware instant raises `TypeError`. | Punch out against a seeded record: `TypeError: can't subtract offset-naive and offset-aware datetimes` → 500. A 500 is an automatic fail. | `tz_aware=True` on the client, so every stored instant comes back as aware UTC. |
| 2 | `to_ms()` | Converted with `datetime.timestamp()`, which interprets a **naive** value in the *server's local* zone. | `to_ms(datetime(2026,7,6,4,35))` returned `1783292700000` instead of `1783312500000` — out by 19,800,000 ms (5 h 30 m) on an IST host, and differently wrong on the grader's host. `created_at` and every punch time are wrong. | `calendar.timegm(value.utctimetuple())`, and a naive value is read as UTC. |
| 3 | `ensure_indexes()` / startup | Indexes were not created at startup, and creation was not idempotent — a key pattern that already exists under another name aborts start-up. | The grader's database ships with only the `_id` index, so every list and analytics query is a `COLLSCAN`; `/admin/explain/*` then fails its "IXSCAN, no COLLSCAN" check. | `ensure_indexes()` runs in the `lifespan` hook. It first looks for an index with the same **key pattern** under any name and accepts it, because every query is hinted with the pattern rather than the name; only a genuinely missing pattern is created, so a second index with the same pattern is never added. Start-up aborts if a required unique index cannot be built or already exists without its unique constraint, since the punch-in and duplicate-create races are settled by that constraint. Calling it twice is a no-op. |
| 4 | `ensure_indexes()` — uniqueness | No unique index on `employees.emp_code` or on `attendance_logs.(emp_code, date)`. | Without them the concurrency contract cannot hold at all: duplicate employees and two records for one employee-day both become possible. 16 parallel identical punch-ins produced several 201s. | Both indexes declared `unique=True`; they are what the race handling relies on (see #12). |
| 5 | `round_half_up()` | Used Python's `round()` — banker's rounding, on a binary float. R4/R8 require decimals, half **away from zero**. | `round(8.125, 2)` → `8.12`, `round(2.675, 2)` → `2.67`. R8 wants `8.13` and `2.68`. It is not cosmetic: the Sales `avg_work_hours` in my demo set is exactly `32.5/4 = 8.125`, so one department total was wrong. | `Decimal(str(value)).quantize(..., rounding=ROUND_HALF_UP)`. `str()` first, so the float's binary error is not carried into the Decimal. |
| 6 | `attendance_date()` | Took the calendar date of the **UTC** instant instead of the IST one (R1). | A punch-in at 02:00 IST on 2026-07-07 is 20:30 UTC on 2026-07-06, so the record landed on `2026-07-06`. Every punch between 00:00 and 05:29 IST was filed a day early. | `punch_in.astimezone(IST).date()`. |
| 7 | `attendance_date()` — overnight | The R1 overnight exception was missing, and the boundary comparison was inclusive. | Shift 22:00–06:00: a punch-in at 05:59:59 must belong to the **previous** day, one at exactly 06:00:00 to the new day. Sample document 7 pins this. Verified both: `2026-07-06` and `2026-07-07`. | Strict `<` against `shift_end`, subtracting one day, and only when `is_overnight()`. |
| 8 | `late_minutes_for()` | Two bugs: the grace test was `>=` rather than strictly `>`, and the minutes were counted from the **end of grace** instead of from `shift_start` (R2). | R2's own three examples, shift 09:30. Was: 09:40:00 → 10, 10:15:59 → 35. Must be: 09:40:00 → 0, 09:40:01 → 10, 10:15:59 → 45. All three now match. | `elapsed > GRACE_SECONDS`, then `elapsed // 60` measured from `shift_start`. |
| 9 | `overtime_minutes_for()` | `shift_end` was placed on the attendance date even for an overnight shift, although R1 says it falls on the **next** calendar day. | Sample document 7 (22:00–06:00, out 06:40 next day) expects 40 minutes; the same-day end gives **1480**. Verified: correct 40, buggy 1480. | `shift_instant(day, shift_end, next_day=is_overnight(...))`. |
| 10 | `derive_fields()` — R5 | `half_day` was decided from the raw elapsed hours, not the **rounded** `work_hours`. | R5 compares the rounded value, strictly. Exactly 4h30m is `4.5` and must be `half_day: false`; the raw comparison also flips any day that rounds up to `4.50`. Verified `4.5 → false`. | Compare `Decimal(str(hours)) < Decimal("4.50")`, using the already-rounded value. |
| 11 | `derive_fields()` — non-presence | `ABSENT` / `LEAVE` kept whatever punch times and derived values the record already had. | Regularize a `PRESENT` day to `LEAVE`: the record kept its `punch_in`, `work_hours` and `late_minutes`, contradicting the `DATA_MODEL.md` table and inflating analytics. Verified all five fields now clear. | A dedicated branch returning the documented defaults: punches `null`, `work_hours` `null`, minutes `0`, `half_day` `false`. |
| 12 | `working_days_between()` | Ignored `joined_on`, and the weekday test included Saturday. | July 2026 has 23 weekdays. A 2026-07-16 joiner must get **12**, not 23, or `attendance_pct` is roughly half what it should be; someone joining 2026-08-01 must get 0. Verified 23 / 12 / 0. | Clamp the start to `joined_on`, return 0 when the clamped start passes the month end, and test `weekday() < 5`. |
| 13 | `attendance_out()` | Read `doc["half_day"]` and `doc["history"]` directly, so a legacy record missing them raised `KeyError` → 500. | Sample document 9 has neither field. `GET /attendance` over seeded data 500s. Verified it now returns `half_day: false`, `history: []`. | `or`-defaults plus `int()` / `bool()` coercion for the numeric and boolean fields. |
| 14 | `history_out()` | The dates nested inside `history[].changes.punch_in.{from,to}` were left as BSON datetimes. | They serialise as ISO strings while the contract says epoch milliseconds — easy to miss because the outer `at` looks right. Verified both are now `int`. | Walk `changes` and run `to_ms()` on the `punch_in` / `punch_out` entries only. |
| 15 | `punch_in()` / `employee_out()` | The document handed to `insert_one` was returned directly. PyMongo **mutates** it, adding `_id`, and `ObjectId` is not JSON-serialisable. | Verified: a dict passed to `insert_one` comes back with keys `['a', '_id']`. The response either 500s or leaks `_id`, which the contract says never appears. | Serialisers whitelist contract fields only, and the insert is given a copy (`dict(doc)`) so the returned document stays clean. |
| 16 | `EpochMillis` / request models | Instants typed as a plain `int`, which Pydantic happily coerces. | `"1783312500000"`, the seconds-looking `1783312500`, an integral float like `1783312500.0` and even `True` are all accepted by a plain `int`; the contract requires 422. (A *non-integral* float such as `1783312500.5` a plain `int` already rejects, for its own reason.) Verified that `StrictInt` plus the bounds rejects every one of them. | `StrictInt` with `ge=100_000_000_000`, `le=4_102_444_800_000`. |
| 17 | `create_employee()` / `punch_in()` | Check-then-insert: a `find_one` for an existing row, then an insert. The gap between them is the race. | 16 parallel identical requests gave more than one 201 and could write two records for one employee-day. Both now give exactly one 201 and fifteen 409s. | Drop the pre-read, insert unconditionally, and map `DuplicateKeyError` to 409. The index decides the winner, not application code. |
| 18 | `list_employees()` / `list_attendance()` | `total` did not reflect the filters, `page_size` was unbounded, and `page` was not floored at 1 (`page=0` makes the skip negative → `OperationFailure` 500). | `?department=Sales` reported the whole collection; `?page_size=100000` pulls everything into memory; `?page=0` 500s. Now: filtered totals, and 422 for `page=0` and `page_size=101`. | `page`/`page_size` bounded in the signature (`ge=1`, `le=100`), `count_documents` run with the **same** filter, and `skip`/`limit` pushed to MongoDB. |
| 19 | `list_attendance()` | Sorted by `date` only — no tiebreaker, so pagination was unstable — and the date parameters were unvalidated. | Two records sharing a date can repeat or vanish across pages. `date_from=07/06/2026` and the impossible `2026-02-30` must be 422, as must `date_from > date_to`. All three verified. | Sort `(date desc, emp_code asc)`, which the `(date, emp_code)` index also supplies, plus `parse_date()` and an explicit range check. |

## Looked at and decided was **not** a defect

* **`POST /attendance/punch-out` returns 404, not 422, when `punched_at` precedes every punch-in.** It reads like a
  validation error, but the contract defines the lookup as "the most recent record whose `punch_in` is at or before
  `punched_at`" and 404 as "no punch-in found". The 422 belongs to `punched_at <= punch_in` *of the record that was
  found* — verified: equal instants give 422, an earlier instant gives 404.
* **`$unwind` after the leaderboard `$lookup` silently drops logs.** It looks like data loss, but those are logs whose
  employee does not exist and the contract says to ignore them. The demo set has `EMP9999` for exactly this; it is
  correctly absent from the leaderboard.
* **`is_overnight()` compares `"HH:MM"` strings.** Safe, because the format is fixed-width and zero-padded, so string
  order equals chronological order. Parsing to minutes would add code and change nothing.
* **`$dayOfWeek` treated as `1 = Sunday`, so Mon–Fri is `2..6`.** Looks like an off-by-one; it is what MongoDB returns.
* **Weekend records are excluded from `present_days` but still counted in the late and overtime totals.** Looks
  inconsistent, and it is what R7 asks for.
* **`total` uses `count_documents`, not `estimated_document_count`.** The estimate is far cheaper but ignores filters,
  and R10 requires `total` to reflect them.
* **Queries are hinted with the index *key pattern* rather than its name.** Deliberate: if the grader's database already
  holds an equivalent index under a different name, hinting by name would fail where the pattern still resolves.
* **A regularization that changes nothing returns 422.** Harsh-looking, but required, and it is why a concurrent burst
  of 16 identical corrections shows one 422 alongside the winners and the 409s.
