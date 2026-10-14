# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the HROne assignment contract (`openapi.yaml`). All application code is in
`app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"        # real env vars take priority over any .env
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

Needs MongoDB 6.0+ (`$densify` and `$setWindowFields`); developed against 8.0. All six indexes are created at
startup, idempotently — no manual step. `GET /health` returns 200 once Mongo answers a ping.

## Layout of `app/main.py`

1. configuration and the Mongo client
2. time and rounding helpers — business rules R1–R5
3. serialisation helpers (BSON dates ⇄ epoch milliseconds)
4. request models and validation
5. query and aggregation **builders**
6. endpoints

Section 5 is the load-bearing choice: each builder returns `(collection, pipeline, index)`, and both the real endpoint
and `/admin/explain/{endpoint}` consume the same builder. The explain output therefore describes the query the endpoint
actually runs, rather than a hand-written copy of it that can drift.

## Test data

`sample_seed.py` loads the nine sample documents from `DATA_MODEL.md`. Two further generators cover what the sample
cannot:

```bash
python tools/seed_demo.py                      # small, deterministic, awkward cases
MONGO_DB=attendance_bulk python tools/seed_bulk.py   # ~93k logs, 400 employees, 6 departments
```

`tools/seed_demo.py` deliberately includes a mid-month joiner, someone who joins after the month, an employee with no
logs at all, a department whose only member has not joined yet, an overnight shift, a weekend record, a half day, an
open record, `ABSENT`/`LEAVE`, a legacy record with no `half_day`/`history`, a log belonging to an employee who does not
exist, and a three-way split on late minutes that produces a tie at a rank cutoff.

## Notes

* **Analytics read the stored derived values** (`work_hours`, `late_minutes`, `overtime_minutes`, `half_day`) and never
  recompute them from the punch times, as `DATA_MODEL.md` requires.
* **Instants** are integer epoch milliseconds at the API boundary and BSON dates in MongoDB, truncated to whole seconds
  (R1). Calendar dates and shift times stay strings.
* **Races** are settled by unique indexes and conditional updates, not by read-then-write: unique `(emp_code, date)` for
  punch-in, a `punch_out: null` guard for punch-out, and an optimistic guard on the before-state plus history length for
  regularization, so no history entry is lost.
* `/admin/explain/*` was checked on the ~93k-record set: every plan uses an `IXSCAN` and none contains a `COLLSCAN`.
* Verified against MongoDB 8.0.8 on the demo and bulk sets — analytics values, status codes, R1–R5 boundaries,
  pagination, and the punch-in / punch-out / regularize races under 16 parallel requests.

`REVIEW.md` lists the defects found in the starter. `DECISIONS.md` answers the five questions.
