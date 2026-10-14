"""Generate a ~100,000 record dataset to check speed and index use.

    MONGO_DB=attendance_bulk python tools/seed_bulk.py

400 employees in 6 departments over 2026-01-01 .. 2026-12-31: weekdays get a record
most of the time, some weekends do, a few employees work an overnight shift, some
join mid-year and some records are left open, ABSENT or LEAVE.
"""

import os
import random
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "People", "Operations"]
EMPLOYEE_COUNT = 400
FIRST_DAY = date(2026, 1, 1)
LAST_DAY = date(2026, 12, 31)


def ist(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=IST).astimezone(
        timezone.utc
    )


def main() -> None:
    random.seed(7)
    db = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)[
        os.getenv("MONGO_DB", "attendance_bulk")
    ]
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})

    people = []
    for index in range(1, EMPLOYEE_COUNT + 1):
        overnight = index % 25 == 0
        joined = FIRST_DAY if index % 7 else date(2026, random.randint(2, 11), random.randint(1, 28))
        people.append(
            {
                "emp_code": f"EMP{index:04d}",
                "name": f"Person {index:04d}",
                "email": f"person{index:04d}@example.com",
                "department": DEPARTMENTS[index % len(DEPARTMENTS)],
                "shift_start": "22:00" if overnight else "09:30",
                "shift_end": "06:00" if overnight else "18:30",
                "joined_on": joined.isoformat(),
                "created_at": ist(joined, 9, 0),
            }
        )
    db.employees.insert_many(people)

    batch, total = [], 0
    for person in people:
        joined = date.fromisoformat(person["joined_on"])
        overnight = person["shift_end"] <= person["shift_start"]
        start_h, start_m = (int(part) for part in person["shift_start"].split(":"))
        day = max(joined, FIRST_DAY)
        while day <= LAST_DAY:
            weekend = day.weekday() >= 5
            roll = random.random()
            if (weekend and roll > 0.08) or (not weekend and roll > 0.93):
                day += timedelta(days=1)
                continue
            if not weekend and roll > 0.86:
                status = "LEAVE" if roll > 0.9 else "ABSENT"
                batch.append(
                    {
                        "emp_code": person["emp_code"],
                        "date": day.isoformat(),
                        "status": status,
                        "punch_in": None,
                        "punch_out": None,
                        "work_hours": None,
                        "late_minutes": 0,
                        "overtime_minutes": 0,
                        "half_day": False,
                        "history": [],
                    }
                )
            else:
                late = random.choice([0, 0, 0, 0, 3, 11, 17, 35, 46, 61])
                punch_in = ist(day, start_h, start_m) + timedelta(minutes=late or random.randint(-8, 10))
                late_minutes = late if late > 10 else 0
                open_record = random.random() < 0.02
                doc = {
                    "emp_code": person["emp_code"],
                    "date": day.isoformat(),
                    "status": random.choice(["PRESENT"] * 8 + ["WFH", "ON_DUTY"]),
                    "punch_in": punch_in,
                    "punch_out": None,
                    "work_hours": None,
                    "late_minutes": late_minutes,
                    "overtime_minutes": 0,
                    "half_day": False,
                    "history": [],
                }
                if not open_record:
                    worked = random.choice([3.5, 4.4, 8.0, 8.5, 9.0, 9.25, 9.5, 10.5])
                    punch_out = punch_in + timedelta(hours=worked)
                    shift_end_day = day + timedelta(days=1) if overnight else day
                    end_h, end_m = (int(part) for part in person["shift_end"].split(":"))
                    overtime = int((punch_out - ist(shift_end_day, end_h, end_m)).total_seconds()) // 60
                    doc.update(
                        {
                            "punch_out": punch_out,
                            "work_hours": round(worked, 2),
                            "overtime_minutes": overtime if overtime >= 30 else 0,
                            "half_day": worked < 4.5,
                        }
                    )
                batch.append(doc)
            day += timedelta(days=1)
        if len(batch) >= 5000:
            db.attendance_logs.insert_many(batch)
            total += len(batch)
            batch = []
    if batch:
        db.attendance_logs.insert_many(batch)
        total += len(batch)

    print(f"seeded {len(people)} employees and {total} attendance logs into '{db.name}'")


if __name__ == "__main__":
    main()
