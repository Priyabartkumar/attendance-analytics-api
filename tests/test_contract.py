"""Contract tests for the Employee Attendance & Analytics API.

Checks the HTTP contract in `openapi.yaml` and the business rules R1-R10 against a
running instance: expected values, not just status codes.

Standard library only - nothing to install beyond `requirements.txt`.

    python tools/seed_demo.py                 # the dataset sections A-B expect
    uvicorn app.main:app --port 8000          # in another shell
    python tests/test_contract.py             # exits non-zero if anything fails

Override the target with BASE_URL, e.g. BASE_URL=http://127.0.0.1:9000.
Sections C-F provision their own employees (EMP7xxx / EMP8xxx) and are independent
of the demo dataset.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
IST = timezone(timedelta(hours=5, minutes=30))

PASSED: list[str] = []
FAILED: list[tuple[str, object, object]] = []


def ms(stamp: str) -> int:
    """An IST wall-clock time as epoch milliseconds."""
    return int(datetime.fromisoformat(stamp).replace(tzinfo=IST).timestamp() * 1000)


def chk(label: str, got: object, want: object) -> None:
    if got == want:
        PASSED.append(label)
        print("  PASS  %s" % label)
    else:
        FAILED.append((label, got, want))
        print("  FAIL  %s" % label)
        print("          got= %r" % (got,))
        print("          want=%r" % (want,))


def call(method: str, path: str, body: object = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE_URL + path, data=data, method=method,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")
    except urllib.error.URLError as exc:
        sys.exit("cannot reach %s (%s) - is the app running?" % (BASE_URL, exc.reason))


def employee(code: str, start: str = "09:30", end: str = "18:30",
             joined: str = "2026-10-01", department: str = "ContractTest") -> None:
    """Create an employee if it is not there already; 409 is fine."""
    call("POST", "/employees", {
        "emp_code": code, "name": "Test %s" % code, "email": "%s@example.com" % code.lower(),
        "department": department, "shift_start": start, "shift_end": end, "joined_on": joined,
    })


def cycle(code: str, punch_in: int, punch_out: int | None = None) -> tuple[int, object]:
    """A fresh employee punching in, and optionally out. Returns the final record."""
    employee(code)
    status, record = call("POST", "/attendance/punch-in",
                          {"emp_code": code, "punched_at": punch_in})
    if punch_out is not None:
        status, record = call("POST", "/attendance/punch-out",
                              {"emp_code": code, "punched_at": punch_out})
    return status, record


def section(title: str) -> None:
    print("\n=== %s ===" % title)


# --------------------------------------------------------------------------- #
# A. The contract surface
# --------------------------------------------------------------------------- #
section("A. health and contract surface")
chk("GET /health is 200", call("GET", "/health")[0], 200)
chk("GET /health body", call("GET", "/health")[1], {"status": "ok"})
chk("GET / is not a route (404)", call("GET", "/")[0], 404)

_, _probe = call("GET", "/employees?department=Engineering")
HAVE_DEMO = isinstance(_probe, dict) and _probe.get("total", 0) >= 3
if not HAVE_DEMO:
    print("\n  !! demo dataset not found - run `python tools/seed_demo.py` first.")
    print("     Sections B and C need it; skipping them.")

# --------------------------------------------------------------------------- #
# B. Analytics values on the demo dataset
# --------------------------------------------------------------------------- #
if HAVE_DEMO:
    section("B. monthly analytics: joiner clamping (R7), presence (R5-R6), R8")
    # attendance_pct is a reported number, so R8's default applies: 2 decimals
    # half-up. The fields that take 4 decimals say so explicitly in their own spec
    # (attendance_rate, moving_avg_7d), and this one does not.
    for code, working_days, present_days, pct in [
        ("EMP0001", 23, 4.0, 17.39),   # full month
        ("EMP0002", 12, 3.5, 29.17),   # joined 2026-07-16, one half day
        ("EMP0003", 23, 2.0, 8.7),     # overnight shift
        ("EMP0004", 0, 0.0, None),     # joined after the month
        ("EMP0010", 23, 0.0, 0.0),     # no logs at all
    ]:
        _, row = call("GET", "/analytics/employees/%s/monthly?month=2026-07" % code)
        chk("%s working_days (R7)" % code, row.get("working_days"), working_days)
        chk("%s present_days (R5-R7)" % code, row.get("present_days"), present_days)
        chk("%s attendance_pct, 2 decimals half-up (R8)" % code, row.get("attendance_pct"), pct)
    _, row = call("GET", "/analytics/employees/EMP0001/monthly?month=2026-07")
    chk("leave_days counts LEAVE records", row.get("leave_days"), 1)
    chk("late_count counts late_minutes > 0", row.get("late_count"), 3)
    chk("total_late_minutes includes weekend records (R7)",
        row.get("total_late_minutes"), 85)
    chk("total_overtime_minutes (R3)", row.get("total_overtime_minutes"), 40)

    section("C. half day, overnight and stored derived values")
    _, page = call("GET", "/attendance?emp_code=EMP0001&date_from=2026-07-04&date_to=2026-07-04")
    half = page["items"][0]
    chk("half day work_hours (R4)", half["work_hours"], 4.0)
    chk("half day flag (R5)", half["half_day"], True)
    chk("late_minutes read from storage", half["late_minutes"], 30)
    _, page = call("GET", "/attendance?emp_code=EMP0003")
    chk("overnight records land on the shift start date (R1)",
        sorted(item["date"] for item in page["items"]), ["2026-07-06", "2026-07-07"])
    chk("overnight work_hours",
        [i["work_hours"] for i in page["items"] if i["date"] == "2026-07-06"][0], 8.75)

    section("D. department summary, leaderboard ties and trend gap filling")
    _, summary = call("GET", "/analytics/departments/summary?month=2026-07")
    by_dept = {item["department"]: item for item in summary["items"]}
    chk("headcount includes employees with no logs (R9)",
        by_dept["Engineering"]["headcount"], 3)
    chk("avg_work_hours over records (R8)", by_dept["Engineering"]["avg_work_hours"], 7.81)
    chk("avg_work_hours rounds .125 half-up (R8)", by_dept["Sales"]["avg_work_hours"], 8.13)
    chk("departments sorted ascending",
        [item["department"] for item in summary["items"]],
        sorted(item["department"] for item in summary["items"]))
    _, board = call("GET", "/analytics/leaderboard/late?month=2026-07&limit=3")
    chk("ties share a rank, next rank skipped",
        [item["rank"] for item in board["items"]], [1, 2, 3, 3])
    chk("limit applies after ranking, so 4 rows for limit=3", len(board["items"]), 4)
    chk("tied rows ordered by emp_code",
        [i["emp_code"] for i in board["items"] if i["rank"] == 3], ["EMP0011", "EMP0012"])
    _, trend = call("GET", "/analytics/departments/Engineering/trend"
                           "?from=2026-07-01&to=2026-07-05")
    chk("one row per calendar day, weekends included", len(trend["items"]), 5)
    chk("weekend attendance_rate is null",
        [item["attendance_rate"] for item in trend["items"]][3:5], [None, None])
    chk("working-day rates present",
        [item["attendance_rate"] for item in trend["items"]][0:3], [0.5, 0.5, 0.5])
    chk("moving_avg_7d on every row",
        all("moving_avg_7d" in item for item in trend["items"]), True)
    chk("is_working_day marks the weekend",
        [item["is_working_day"] for item in trend["items"]], [True, True, True, False, False])

    section("E. pagination (R10) and filters")
    collected: list[tuple[str, str]] = []
    page_number, total = 0, None
    while True:
        page_number += 1
        _, page = call("GET", "/attendance?date_from=2026-07-01&date_to=2026-07-31"
                              "&page=%d&page_size=4" % page_number)
        total = page["total"]
        collected += [(item["emp_code"], item["date"]) for item in page["items"]]
        if not page["items"] or len(collected) >= total:
            break
    chk("pages do not overlap", len(collected), len(set(collected)))
    chk("pages cover the filtered total", len(collected), total)
    chk("total reflects the status filter (R10)",
        call("GET", "/attendance?status=LEAVE&date_from=2026-07-01&date_to=2026-07-31")[1]["total"], 2)
    chk("ABSENT is distinct from a missing record",
        call("GET", "/attendance?status=ABSENT&date_from=2026-07-01&date_to=2026-07-31")[1]["total"], 1)
    chk("records sorted by date descending",
        (lambda d: d == sorted(d, reverse=True))(
            [i["date"] for i in call("GET", "/attendance?page_size=100")[1]["items"]]), True)

# --------------------------------------------------------------------------- #
# F. Business-rule boundaries, from the examples in the rules themselves
# --------------------------------------------------------------------------- #
section("F. R1 time zone, attendance date and whole-second truncation")
_, record = cycle("EMP8101", ms("2026-11-02T09:35:00") + 900)
chk("sub-second precision is truncated, not rounded",
    record.get("punch_in"), ms("2026-11-02T09:35:00"))
chk("date is the IST calendar date of punch_in", record.get("date"), "2026-11-02")
_, record = cycle("EMP8102", ms("2026-11-03T00:30:00"))
chk("00:30 IST stays on the IST date, not the UTC one", record.get("date"), "2026-11-03")

section("F. R1 overnight shift 22:00-06:00")
employee("EMP8103", start="22:00", end="06:00")
_, record = call("POST", "/attendance/punch-in",
                 {"emp_code": "EMP8103", "punched_at": ms("2026-11-03T02:00:00")})
chk("punch-in before shift_end belongs to the previous day", record.get("date"), "2026-11-02")
status, record = call("POST", "/attendance/punch-out",
                      {"emp_code": "EMP8103", "punched_at": ms("2026-11-03T06:00:00")})
chk("overnight shift closes on the next calendar day",
    (status, record.get("work_hours")), (200, 4.0))
chk("overnight overtime measured from the next-day shift_end (R3)",
    record.get("overtime_minutes"), 0)

section("F. R2 late: grace is 10 minutes, counted from shift_start")
for code, clock, want in [("EMP8111", "09:40:00", 0),      # exactly 10:00 -> not late
                          ("EMP8112", "09:40:01", 10),     # one second past -> 10
                          ("EMP8113", "10:15:59", 45)]:    # floor, from shift_start
    _, record = cycle(code, ms("2026-11-02T%s" % clock))
    chk("shift 09:30, punch-in %s -> late_minutes %d" % (clock, want),
        record.get("late_minutes"), want)
_, record = cycle("EMP8114", ms("2026-11-02T09:00:00"))
chk("an early punch-in is never negative", record.get("late_minutes"), 0)

section("F. R3 overtime: floor minutes, only when it reaches 30")
for code, clock, want in [("EMP8121", "18:59:00", 0),
                          ("EMP8122", "19:00:00", 30),
                          ("EMP8123", "19:00:59", 30)]:
    _, record = cycle(code, ms("2026-11-02T09:30:00"), ms("2026-11-02T%s" % clock))
    chk("shift ends 18:30, punch-out %s -> overtime %d" % (clock, want),
        record.get("overtime_minutes"), want)

section("F. R4 work_hours: 2 decimals, half away from zero")
_, record = cycle("EMP8131", ms("2026-11-02T09:30:00"), ms("2026-11-02T17:37:30"))
chk("8.125 h rounds half-UP to 8.13, not 8.12", record.get("work_hours"), 8.13)
_, record = cycle("EMP8132", ms("2026-11-02T09:30:00"), ms("2026-11-02T18:30:00"))
chk("exactly 9 h", record.get("work_hours"), 9.0)

section("F. R5 half_day: compares the ROUNDED work_hours against 4.50")
_, record = cycle("EMP8141", ms("2026-11-02T09:30:00"), ms("2026-11-02T14:00:00"))
chk("exactly 4.50 h is not a half day",
    (record.get("work_hours"), record.get("half_day")), (4.5, False))
_, record = cycle("EMP8142", ms("2026-11-02T09:30:00"), ms("2026-11-02T13:59:42"))
chk("4.495 h rounds up to 4.50, so not a half day",
    (record.get("work_hours"), record.get("half_day")), (4.5, False))
_, record = cycle("EMP8143", ms("2026-11-02T09:30:00"), ms("2026-11-02T13:59:24"))
chk("exactly 4.49 h is a half day",
    (record.get("work_hours"), record.get("half_day")), (4.49, True))

section("F. R10 pagination bounds")
chk("page_size defaults to 20", call("GET", "/attendance")[1].get("page_size"), 20)
chk("page_size=100 is accepted", call("GET", "/attendance?page_size=100")[0], 200)
chk("page_size=101 is 422", call("GET", "/attendance?page_size=101")[0], 422)
chk("page=0 is 422", call("GET", "/attendance?page=0")[0], 422)
chk("employees page_size=101 is 422", call("GET", "/employees?page_size=101")[0], 422)

# --------------------------------------------------------------------------- #
# G. Status codes and validation
# --------------------------------------------------------------------------- #
section("G. instants are integer epoch milliseconds (422 otherwise)")
employee("EMP8151")
for label, value in [("a float", 1792038120000.5),
                     ("a string", "1792038120000"),
                     ("a seconds-looking value", 1792038120),
                     ("above the maximum", 4102444800001)]:
    chk("punched_at as %s is 422" % label,
        call("POST", "/attendance/punch-in",
             {"emp_code": "EMP8151", "punched_at": value})[0], 422)

section("G. punch-out selection and status codes")
employee("EMP8161")
call("POST", "/attendance/punch-in", {"emp_code": "EMP8161", "punched_at": ms("2026-11-02T09:30:00")})
call("POST", "/attendance/punch-out", {"emp_code": "EMP8161", "punched_at": ms("2026-11-02T18:30:00")})
chk("a second punch-out is 409 (already punched out)",
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP8161", "punched_at": ms("2026-11-02T19:00:00")})[0], 409)
employee("EMP8162")
chk("punch-out with no punch-in at all is 404",
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP8162", "punched_at": ms("2026-11-02T18:30:00")})[0], 404)
chk("punch-out for an unknown employee is 404",
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP8999", "punched_at": ms("2026-11-02T18:30:00")})[0], 404)
employee("EMP8163")
call("POST", "/attendance/punch-in", {"emp_code": "EMP8163", "punched_at": ms("2026-11-02T09:30:00")})
chk("punched_at equal to punch_in is 422",
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP8163", "punched_at": ms("2026-11-02T09:30:00")})[0], 422)
chk("more than 24 hours after punch_in is 422",
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP8163", "punched_at": ms("2026-11-03T09:31:00")})[0], 422)
# The contract selects the most recent record with punch_in <= punched_at. With an
# older record still open and a newer one closed, that is the closed one -> 409.
employee("EMP8164")
call("POST", "/attendance/punch-in", {"emp_code": "EMP8164", "punched_at": ms("2026-11-02T09:30:00")})
call("POST", "/attendance/punch-in", {"emp_code": "EMP8164", "punched_at": ms("2026-11-03T09:30:00")})
call("POST", "/attendance/punch-out", {"emp_code": "EMP8164", "punched_at": ms("2026-11-03T18:30:00")})
chk("an older open record does not shadow the most recent one",
    call("POST", "/attendance/punch-out",
         {"emp_code": "EMP8164", "punched_at": ms("2026-11-03T19:00:00")})[0], 409)

section("G. punch-in status codes")
chk("punch-in for an unknown employee is 404",
    call("POST", "/attendance/punch-in", {"emp_code": "EMP8999"})[0], 404)
chk("a non-presence status is rejected",
    call("POST", "/attendance/punch-in",
         {"emp_code": "EMP8151", "status": "LEAVE"})[0], 422)

section("G. regularize validation")
employee("EMP8171")
call("POST", "/attendance/punch-in", {"emp_code": "EMP8171", "punched_at": ms("2026-11-02T09:30:00")})
chk("ABSENT supplied together with punch_in is 422",
    call("PATCH", "/attendance/EMP8171/2026-11-02",
         {"status": "ABSENT", "punch_in": ms("2026-11-02T09:30:00"),
          "reason": "bad combination", "regularized_by": "A"})[0], 422)
chk("moving punch_in off the record's date is 422",
    call("PATCH", "/attendance/EMP8171/2026-11-02",
         {"punch_in": ms("2026-11-05T09:30:00"), "reason": "wrong day",
          "regularized_by": "A"})[0], 422)
chk("a request that changes nothing is 422",
    call("PATCH", "/attendance/EMP8171/2026-11-02",
         {"status": "PRESENT", "reason": "no change at all", "regularized_by": "A"})[0], 422)
chk("neither status nor punch times is 422",
    call("PATCH", "/attendance/EMP8171/2026-11-02",
         {"reason": "nothing given", "regularized_by": "A"})[0], 422)
chk("too short a reason is 422",
    call("PATCH", "/attendance/EMP8171/2026-11-02",
         {"status": "WFH", "reason": "no", "regularized_by": "A"})[0], 422)
chk("an unknown employee is 404",
    call("PATCH", "/attendance/EMP8999/2026-11-02",
         {"status": "LEAVE", "reason": "unknown employee", "regularized_by": "A"})[0], 404)
chk("a date with no record is 404",
    call("PATCH", "/attendance/EMP8171/2026-11-30",
         {"status": "LEAVE", "reason": "no record there", "regularized_by": "A"})[0], 404)
status, record = call("PATCH", "/attendance/EMP8171/2026-11-02",
                      {"status": "LEAVE", "reason": "approved leave", "regularized_by": "A"})
chk("LEAVE clears both punch times",
    (status, record.get("punch_in"), record.get("punch_out")), (200, None, None))
chk("history keeps only the fields that actually changed",
    sorted(record["history"][0]["changes"]), ["punch_in", "status"])
chk("history entry carries who and why",
    (record["history"][0]["by"], record["history"][0]["reason"]), ("A", "approved leave"))

section("G. employee validation")
chk("shift_start equal to shift_end is 422",
    call("POST", "/employees", {"emp_code": "EMP8181", "name": "T", "email": "a@b.com",
         "department": "ContractTest", "shift_start": "09:30", "shift_end": "09:30",
         "joined_on": "2026-10-01"})[0], 422)
chk("an emp_code outside the pattern is 422",
    call("POST", "/employees", {"emp_code": "XX1", "name": "T", "email": "a@b.com",
         "department": "ContractTest", "joined_on": "2026-10-01"})[0], 422)
chk("a malformed email is 422",
    call("POST", "/employees", {"emp_code": "EMP8182", "name": "T", "email": "nope",
         "department": "ContractTest", "joined_on": "2026-10-01"})[0], 422)
chk("an impossible joined_on is 422",
    call("POST", "/employees", {"emp_code": "EMP8183", "name": "T", "email": "a@b.com",
         "department": "ContractTest", "joined_on": "2026-02-30"})[0], 422)
chk("a duplicate emp_code is 409",
    call("POST", "/employees", {"emp_code": "EMP8171", "name": "T", "email": "a@b.com",
         "department": "ContractTest", "joined_on": "2026-10-01"})[0], 409)

section("G. list, trend and monthly status codes")
chk("date_from after date_to is 422",
    call("GET", "/attendance?date_from=2026-07-31&date_to=2026-07-01")[0], 422)
chk("a malformed date is 422", call("GET", "/attendance?date_from=31-07-2026")[0], 422)
chk("an impossible date is 422", call("GET", "/attendance?date_from=2026-02-30")[0], 422)
chk("an unknown status is 422", call("GET", "/attendance?status=NOPE")[0], 422)
chk("an unknown department is 404",
    call("GET", "/analytics/departments/NoSuchDept/trend?from=2026-07-01&to=2026-07-05")[0], 404)
chk("to before from is 422",
    call("GET", "/analytics/departments/ContractTest/trend?from=2026-07-10&to=2026-07-01")[0], 422)
chk("a range over 92 days is 422",
    call("GET", "/analytics/departments/ContractTest/trend?from=2026-01-01&to=2026-12-31")[0], 422)
chk("exactly 92 days is accepted",
    call("GET", "/analytics/departments/ContractTest/trend?from=2026-07-01&to=2026-09-30")[0], 200)
chk("monthly for an unknown employee is 404",
    call("GET", "/analytics/employees/EMP8999/monthly?month=2026-07")[0], 404)
chk("a malformed month is 422",
    call("GET", "/analytics/employees/EMP8171/monthly?month=2026-13")[0], 422)

# --------------------------------------------------------------------------- #
# H. The explain endpoint
# --------------------------------------------------------------------------- #
section("H. explain: index scans, no collection scans")
EXPLAIN_CASES = [
    ("attendance_list", "?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-31"),
    ("attendance_list", "?date_from=2026-07-01&date_to=2026-07-31"),
    ("employee_monthly", "?emp_code=EMP0001&month=2026-07"),
    ("department_summary", "?month=2026-07"),
    ("department_summary", "?month=2026-07&department=Engineering"),
    ("late_leaderboard", "?month=2026-07"),
    ("late_leaderboard", "?month=2026-07&department=Engineering"),
    ("department_trend", "?department=ContractTest&from=2026-07-01&to=2026-07-31"),
]
for name, query in EXPLAIN_CASES:
    status, payload = call("GET", "/admin/explain/%s%s" % (name, query))
    blob = json.dumps(payload)
    chk("%s%s -> IXSCAN, no COLLSCAN" % (name, query[:34]),
        (status, "IXSCAN" in blob, "COLLSCAN" in blob), (200, True, False))
status, payload = call("GET", "/admin/explain/attendance_list?emp_code=EMP0001")
chk("explain response shape", sorted(payload), ["collection", "endpoint", "explain"])
chk("explain ran with executionStats verbosity",
    "executionStats" in json.dumps(payload["explain"]), True)
chk("explain with a missing required parameter is 422",
    call("GET", "/admin/explain/employee_monthly?emp_code=EMP0001")[0], 422)
chk("explain for an unknown endpoint name is 422", call("GET", "/admin/explain/nope")[0], 422)

# --------------------------------------------------------------------------- #
# I. Concurrency
# --------------------------------------------------------------------------- #
PARALLEL = 16
section("I. races: %d parallel requests each" % PARALLEL)
creation = {"emp_code": "EMP7001", "name": "Race", "email": "emp7001@example.com",
            "department": "RaceTest", "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-10-01"}
with ThreadPoolExecutor(PARALLEL) as pool:
    codes = Counter(f.result()[0] for f in
                    [pool.submit(call, "POST", "/employees", creation) for _ in range(PARALLEL)])
chk("employee creation: exactly one 201", codes[201], 1)
chk("employee creation: the rest are 409", codes[409], PARALLEL - 1)

PUNCH_IN, PUNCH_OUT = ms("2026-11-10T09:30:00"), ms("2026-11-10T18:30:00")
with ThreadPoolExecutor(PARALLEL) as pool:
    codes = Counter(f.result()[0] for f in
                    [pool.submit(call, "POST", "/attendance/punch-in",
                                 {"emp_code": "EMP7001", "punched_at": PUNCH_IN})
                     for _ in range(PARALLEL)])
chk("punch-in: exactly one 201", codes[201], 1)
chk("punch-in: the rest are 409", codes[409], PARALLEL - 1)
chk("punch-in: no 500s", sum(v for k, v in codes.items() if k >= 500), 0)
chk("punch-in: only one record exists for that employee and date",
    call("GET", "/attendance?emp_code=EMP7001&date_from=2026-11-10&date_to=2026-11-10")[1]["total"], 1)

with ThreadPoolExecutor(PARALLEL) as pool:
    codes = Counter(f.result()[0] for f in
                    [pool.submit(call, "POST", "/attendance/punch-out",
                                 {"emp_code": "EMP7001", "punched_at": PUNCH_OUT})
                     for _ in range(PARALLEL)])
chk("punch-out: exactly one 200", codes[200], 1)
chk("punch-out: no 500s", sum(v for k, v in codes.items() if k >= 500), 0)

with ThreadPoolExecutor(PARALLEL) as pool:
    codes = Counter(f.result()[0] for f in
                    [pool.submit(call, "PATCH", "/attendance/EMP7001/2026-11-10",
                                 {"status": "WFH", "reason": "parallel %d" % i,
                                  "regularized_by": "A"})
                     for i in range(PARALLEL)])
chk("regularize: exactly one 200", codes[200], 1)
chk("regularize: no 500s", sum(v for k, v in codes.items() if k >= 500), 0)
_, page = call("GET", "/attendance?emp_code=EMP7001&date_from=2026-11-10&date_to=2026-11-10")
chk("regularize: history holds exactly the winning entry",
    len(page["items"][0]["history"]), 1)

section("I. sequential corrections accumulate and are never overwritten")
_, record = call("PATCH", "/attendance/EMP7001/2026-11-10",
                 {"status": "ON_DUTY", "reason": "second correction", "regularized_by": "B"})
chk("history has 2 entries", len(record["history"]), 2)
_, record = call("PATCH", "/attendance/EMP7001/2026-11-10",
                 {"status": "PRESENT", "reason": "third correction", "regularized_by": "C"})
chk("history has 3 entries", len(record["history"]), 3)
chk("entries are in append order", [h["by"] for h in record["history"]], ["A", "B", "C"])
chk("the first entry survived", record["history"][0]["reason"].startswith("parallel"), True)
chk("every entry records a real transition",
    all(h["changes"] for h in record["history"]), True)

# --------------------------------------------------------------------------- #
print("\n" + "=" * 62)
print("  %d checks: %d passed, %d failed" % (len(PASSED) + len(FAILED), len(PASSED), len(FAILED)))
for label, got, want in FAILED:
    print("   FAIL %s\n        got=%r want=%r" % (label, got, want))
print("=" * 62)
sys.exit(1 if FAILED else 0)
