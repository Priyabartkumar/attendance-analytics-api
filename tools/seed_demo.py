"""Seed a small, deterministic dataset with the awkward cases the rules describe.

    python tools/seed_demo.py            # uses MONGO_URI / MONGO_DB (or a local .env)

Covers: a mid-month joiner, an employee who joins after the month, an employee with
no logs at all, a whole department that has not joined yet, an overnight shift, a
weekend record, a half day, an open record, ABSENT / LEAVE, a legacy record without
`half_day` / `history`, a log for an employee who does not exist, and a three-way
tie on late minutes.  Everything is in 2026-07 (July 2026 starts on a Wednesday).
"""

import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))


def ist(day: str, hhmmss: str) -> datetime:
    """An IST wall-clock time as the UTC instant MongoDB stores."""
    stamp = datetime.fromisoformat(f"{day}T{hhmmss}").replace(tzinfo=IST)
    return stamp.astimezone(timezone.utc)


def employee(code, name, dept, joined, start="09:30", end="18:30"):
    return {
        "emp_code": code,
        "name": name,
        "email": f"{code.lower()}@example.com",
        "department": dept,
        "shift_start": start,
        "shift_end": end,
        "joined_on": joined,
        "created_at": ist(joined, "09:00:00"),
    }


def log(code, day, status="PRESENT", punch_in=None, punch_out=None, work_hours=None,
        late=0, overtime=0, half_day=False, legacy=False):
    doc = {
        "emp_code": code,
        "date": day,
        "status": status,
        "punch_in": ist(day, punch_in) if punch_in else None,
        "punch_out": None,
        "work_hours": work_hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
        "history": [],
    }
    if punch_out:
        out_day, _, out_time = punch_out.partition(" ") if " " in punch_out else ("", "", punch_out)
        doc["punch_out"] = ist(out_day or day, out_time)
    if legacy:  # older records omit these two fields entirely
        doc.pop("half_day")
        doc.pop("history")
    return doc


EMPLOYEES = [
    # Engineering
    employee("EMP0001", "Asha Rao", "Engineering", "2026-01-05"),
    employee("EMP0002", "Bala Iyer", "Engineering", "2026-07-16"),          # mid-month joiner
    employee("EMP0003", "Chitra Das", "Engineering", "2026-06-01", "22:00", "06:00"),  # overnight
    employee("EMP0004", "Dev Menon", "Engineering", "2026-08-01"),         # joins after July
    # Sales
    employee("EMP0010", "Esha Khan", "Sales", "2026-02-01"),               # no logs at all
    employee("EMP0011", "Farid Ali", "Sales", "2026-02-01"),
    employee("EMP0012", "Gita Nair", "Sales", "2026-02-01"),
    employee("EMP0013", "Hari Bose", "Sales", "2026-02-01"),
    # a department whose only employee joins in September
    employee("EMP0020", "Iqbal Shah", "Future", "2026-09-01"),
]

LOGS = [
    # EMP0001: full days, one late day, one weekend day, one LEAVE, one missing weekday
    log("EMP0001", "2026-07-01", punch_in="09:28:00", punch_out="18:35:00", work_hours=9.12),
    log("EMP0001", "2026-07-02", punch_in="10:15:59", punch_out="19:05:00", work_hours=8.82, late=45),
    log("EMP0001", "2026-07-03", punch_in="09:40:01", punch_out="19:10:00", work_hours=9.5,
        late=10, overtime=40),
    log("EMP0001", "2026-07-04", punch_in="10:00:00", punch_out="14:00:00", work_hours=4.0,
        late=30, half_day=True),                                           # Saturday + half day
    log("EMP0001", "2026-07-06", status="LEAVE"),
    # 2026-07-07 deliberately has no record at all
    log("EMP0001", "2026-07-08", punch_in="09:30:00", punch_out="18:30:00", work_hours=9.0),

    # EMP0002 joined 2026-07-16: WFH, ON_DUTY, ABSENT, an open record and a legacy record
    log("EMP0002", "2026-07-16", status="WFH", punch_in="09:35:00", punch_out="13:00:00",
        work_hours=3.42, half_day=True),
    log("EMP0002", "2026-07-17", status="ON_DUTY", punch_in="09:45:00", punch_out="18:40:00",
        work_hours=8.92, late=15),
    log("EMP0002", "2026-07-20", status="ABSENT"),
    log("EMP0002", "2026-07-21", punch_in="09:50:00", work_hours=None, late=20),  # still open
    log("EMP0002", "2026-07-22", punch_in="09:31:00", punch_out="18:33:00", work_hours=9.03,
        legacy=True),

    # EMP0003 overnight 22:00-06:00: the date is the day the shift started
    log("EMP0003", "2026-07-06", punch_in="21:55:00", punch_out="2026-07-07 06:40:00",
        work_hours=8.75, overtime=40),
    log("EMP0003", "2026-07-07", punch_in="22:30:00", punch_out="2026-07-08 06:00:00",
        work_hours=7.5, late=30),

    # Sales: EMP0011 and EMP0012 tie on 60 late minutes, EMP0013 has 90
    log("EMP0011", "2026-07-01", punch_in="10:00:00", punch_out="18:30:00", work_hours=8.5, late=30),
    log("EMP0011", "2026-07-02", punch_in="10:00:00", punch_out="18:30:00", work_hours=8.5, late=30),
    log("EMP0012", "2026-07-01", punch_in="10:30:00", punch_out="18:30:00", work_hours=8.0, late=60),
    log("EMP0013", "2026-07-01", punch_in="11:00:00", punch_out="18:30:00", work_hours=7.5, late=90),
    log("EMP0013", "2026-07-02", status="LEAVE"),

    # a log whose employee does not exist: analytics must ignore it
    log("EMP9999", "2026-07-01", punch_in="12:00:00", punch_out="18:30:00", work_hours=6.5, late=150),
]


def main() -> None:
    db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[
        os.getenv("MONGO_DB", "attendance_db")
    ]
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many(EMPLOYEES)
    db.attendance_logs.insert_many(LOGS)
    print(f"seeded {len(EMPLOYEES)} employees and {len(LOGS)} logs into '{db.name}'")


if __name__ == "__main__":
    main()
