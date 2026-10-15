"""
Employee Attendance & Analytics API (FastAPI + MongoDB).

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (real environment variables win over a local .env)

Layout of this file
  1. configuration and the Mongo client
  2. time / rounding helpers that implement the business rules R1-R5
  3. serialisation helpers (MongoDB <-> HTTP epoch milliseconds)
  4. request models and validation helpers
  5. query and aggregation builders - shared by the endpoints and /admin/explain
  6. endpoints

All instants are stored as BSON dates (UTC, whole seconds) and exchanged as integer
epoch milliseconds. Calendar dates ("YYYY-MM-DD") and shift times ("HH:MM") are IST
strings both in MongoDB and in the API.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override real environment variables, which is what the contract asks for

log = logging.getLogger("attendance")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: without it PyMongo hands back naive datetimes and every comparison
# against an IST-aware instant raises TypeError.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000, appname="attendance-api")
db = client[MONGO_DB]
employees = db["employees"]
attendance_logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
NON_PRESENCE_STATUSES = ("ABSENT", "LEAVE")
ALL_STATUSES = PRESENCE_STATUSES + NON_PRESENCE_STATUSES

GRACE_SECONDS = 10 * 60          # R2
MIN_OVERTIME_MINUTES = 30        # R3
HALF_DAY_HOURS = Decimal("4.50")  # R5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"

# Index name -> (keys, options). Created at startup, idempotently.
INDEXES: dict[str, tuple[str, list[tuple[str, int]], dict[str, Any]]] = {
    # employees
    "emp_code_unique": ("employees", [("emp_code", ASCENDING)], {"unique": True}),
    "dept_joined": ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], {}),
    "dept_emp": ("employees", [("department", ASCENDING), ("emp_code", ASCENDING)], {}),
    # attendance_logs
    "emp_date_unique": (
        "attendance_logs",
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        {"unique": True},
    ),
    "date_emp": ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], {}),
    "emp_punch_in": ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], {}),
}


def hint(name: str) -> list[tuple[str, int]]:
    """The key pattern of one of our indexes. Queries are hinted with the pattern
    rather than the name so a differently named equivalent index still works."""
    return INDEXES[name][1]


def ensure_indexes() -> None:
    """Create every index this app relies on. Safe to call repeatedly."""
    for name, (coll, keys, opts) in INDEXES.items():
        try:
            db[coll].create_index(keys, name=name, **opts)
            log.info("Index created or verified: %s",name)
        except PyMongoError:
            # A unique index cannot be built when the seeded data already violates it,
            # and the name may be taken by an equivalent index. Either way the key
            # pattern must exist, because the queries below are hinted with it.
            log.exception("Failed to create required index %s on %s", name, coll,)
            raise
            


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # pragma: no cover - never block start-up on the database
        log.exception("Application startup failed: required MongoDB indexes"
                      "could not be created."
        )
        raise
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Business rules R1-R5
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Current instant, truncated to whole seconds (R1)."""
    return datetime.now(UTC).replace(microsecond=0)


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime truncated to whole seconds (R1)."""
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def to_ms(value: Optional[datetime]) -> Optional[int]:
    """BSON date -> epoch milliseconds. Naive values are read as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return calendar.timegm(value.utctimetuple()) * 1000 + value.microsecond // 1000


def round_half_up(value: Decimal | float | int, places: int) -> float:
    """R8: decimal rounding, half away from zero - not float/banker's rounding."""
    quant = Decimal(1).scaleb(-places)
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(dec.quantize(quant, rounding=ROUND_HALF_UP))


def parse_hhmm(hhmm: str) -> tuple[int, int]:
    hours, minutes = hhmm.split(":")
    return int(hours), int(minutes)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means the shift runs past midnight."""
    return shift_end <= shift_start


def shift_instant(day: str, hhmm: str, next_day: bool = False) -> datetime:
    """The IST instant of a shift boundary on the attendance day `day`."""
    hours, minutes = parse_hhmm(hhmm)
    base = date_cls.fromisoformat(day)
    if next_day:
        base += timedelta(days=1)
    return datetime(base.year, base.month, base.day, hours, minutes, tzinfo=IST)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: the IST calendar date a punch-in belongs to."""
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.strftime("%H:%M:%S") < f"{shift_end}:00":
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def late_minutes_for(punch_in: datetime, day: str, shift_start: str) -> int:
    """R2: late only when strictly more than 10:00 after shift_start; then floor(minutes)."""
    elapsed = int((punch_in - shift_instant(day, shift_start)).total_seconds())
    return elapsed // 60 if elapsed > GRACE_SECONDS else 0


def overtime_minutes_for(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:
    """R3: floor(minutes) after shift_end, kept only when it reaches 30."""
    end = shift_instant(day, shift_end, next_day=is_overnight(shift_start, shift_end))
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def work_hours_for(punch_in: datetime, punch_out: datetime) -> float:
    """R4: elapsed seconds / 3600, 2 decimals half-up."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def derive_fields(
    status: str,
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    day: str,
    emp: dict,
) -> dict[str, Any]:
    """Every stored derived value for one record, from the final punch times (R2-R5)."""
    if status in NON_PRESENCE_STATUSES:
        return {
            "status": status,
            "punch_in": None,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
        }
    out = {
        "status": status,
        "punch_in": punch_in,
        "punch_out": punch_out,
        "work_hours": None,
        "late_minutes": late_minutes_for(punch_in, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        hours = work_hours_for(punch_in, punch_out)
        out["work_hours"] = hours
        out["overtime_minutes"] = overtime_minutes_for(
            punch_out, day, emp["shift_start"], emp["shift_end"]
        )
        out["half_day"] = Decimal(str(hours)) < HALF_DAY_HOURS  # R5 uses the rounded value
    return out


def month_range(month: str) -> tuple[str, str]:
    """'YYYY-MM' -> inclusive first and last calendar day of that month."""
    year, mon = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def working_days_between(first: date_cls, last: date_cls, joined_on: Optional[str]) -> int:
    """R7: Mon-Fri days in [first, last], never before the employee joined."""
    start = first
    if joined_on:
        try:
            joined = date_cls.fromisoformat(joined_on)
        except ValueError:
            joined = first
        start = max(start, joined)
    if start > last:
        return 0
    return sum(
        1
        for offset in range((last - start).days + 1)
        if (start + timedelta(days=offset)).weekday() < 5
    )


# --------------------------------------------------------------------------- #
# 3. Serialisation
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def history_out(entry: dict) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    for field, delta in (entry.get("changes") or {}).items():
        if field in ("punch_in", "punch_out"):
            changes[field] = {"from": to_ms(delta.get("from")), "to": to_ms(delta.get("to"))}
        else:
            changes[field] = {"from": delta.get("from"), "to": delta.get("to")}
    return {
        "at": to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def attendance_out(doc: dict) -> dict[str, Any]:
    """An attendance record as the contract defines it: no _id, instants in epoch ms,
    missing legacy fields defaulted."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(entry) for entry in doc.get("history") or []],
    }


def jsonable(value: Any) -> Any:
    """Any BSON document as plain JSON (used for raw explain output)."""
    return json.loads(json_util.dumps(value))


# --------------------------------------------------------------------------- #
# 4. Request models and validation
# --------------------------------------------------------------------------- #
# Instants are integer epoch milliseconds: strings, floats and seconds-looking
# values must be rejected with 422, so StrictInt plus explicit bounds.
EpochMillis = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


def invalid(loc: list[Any], msg: str) -> HTTPException:
    """422 in FastAPI's validation-error shape."""
    return HTTPException(status_code=422, detail=[{"loc": loc, "msg": msg, "type": "value_error"}])


def parse_date(value: str, where: str, name: str) -> date_cls:
    import re

    if not re.match(DATE_RE, value or ""):
        raise invalid([where, name], "expected a date as YYYY-MM-DD")
    try:
        return date_cls.fromisoformat(value)
    except ValueError:
        raise invalid([where, name], "not a real calendar date")


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_a_date(cls, value: str) -> str:
        import re

        if not re.match(DATE_RE, value):
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            date_cls.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on is not a real calendar date")
        return value

    @model_validator(mode="after")
    def _shift_is_not_empty(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


StatusQuery = Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]]
PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]


# --------------------------------------------------------------------------- #
# 5. Query and aggregation builders
#    Each builder returns (collection, query/pipeline, options) so that the real
#    endpoint and /admin/explain run exactly the same thing.
# --------------------------------------------------------------------------- #
def _dow(date_expr: Any) -> dict:
    """Day of week (1 = Sunday .. 7 = Saturday) of a 'YYYY-MM-DD' expression."""
    return {
        "$dayOfWeek": {
            "date": {"$dateFromString": {"dateString": date_expr, "format": "%Y-%m-%d", "timezone": "UTC"}},
            "timezone": "UTC",
        }
    }


_IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}
# A present day counts 2 halves, a half day 1 (kept as integers so the maths stays exact).
_HALVES = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}


def attendance_list_query(
    emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]
) -> tuple[dict, list[tuple[str, int]], str]:
    """Filter, sort and the index that serves them for GET /attendance."""
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        window: dict[str, str] = {}
        if date_from:
            window["$gte"] = date_from
        if date_to:
            window["$lte"] = date_to
        query["date"] = window
    if status:
        query["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    # With an emp_code the (emp_code, date) index is the selective one; otherwise the
    # (date desc, emp_code asc) index also supplies the sort order, so there is no
    # collection scan and no blocking sort at 100k documents.
    index = "emp_date_unique" if emp_code else "date_emp"
    return query, sort, index


def employee_monthly_pipeline(emp_code: str, month: str) -> tuple[str, list[dict], str]:
    first, last = month_range(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$set": {"_dow": _dow("$date")}},
        {
            "$group": {
                "_id": None,
                # R7: present_days only counts Mon-Fri records, R5: a half day is 0.5
                "present_halves": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    _IS_PRESENT,
                                    {"$gte": ["$_dow", 2]},
                                    {"$lte": ["$_dow", 6]},
                                ]
                            },
                            _HALVES,
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    return "attendance_logs", pipeline, "emp_date_unique"


def department_summary_pipeline(month: str, department: Optional[str]) -> tuple[str, list[dict], str]:
    """Starts from `employees`, so employees with no logs are counted (R9) and the
    department filter is applied before the join."""
    first, last = month_range(month)
    match: dict[str, Any] = {"joined_on": {"$lte": last}}
    if department:
        match["department"] = department

    def per_log(expression: Any) -> dict:
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expression}}}

    present = {"$in": ["$$l.status", list(PRESENCE_STATUSES)]}
    halves = {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 1, 2]}
    has_hours = {"$and": [present, {"$ne": [{"$ifNull": ["$$l.work_hours", None]}, None]}]}

    pipeline = [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                            "work_hours": 1,
                            "dow": _dow("$date"),
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": 1,
                "present_halves": per_log(
                    {
                        "$cond": [
                            {
                                "$and": [
                                    present,
                                    {"$gte": ["$$l.dow", 2]},
                                    {"$lte": ["$$l.dow", 6]},
                                ]
                            },
                            halves,
                            0,
                        ]
                    }
                ),
                # work_hours are 2-decimal values; summing them as integer "cents"
                # keeps the mean exact so the half-up rounding is reliable.
                "work_cents": per_log(
                    {"$cond": [has_hours, {"$round": [{"$multiply": ["$$l.work_hours", 100]}, 0]}, 0]}
                ),
                "work_records": per_log({"$cond": [has_hours, 1, 0]}),
                "late_count": per_log(
                    {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]}
                ),
                "total_late_minutes": per_log({"$ifNull": ["$$l.late_minutes", 0]}),
                "leave_count": per_log({"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}),
                "on_duty_count": per_log({"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}),
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_halves": {"$sum": "$present_halves"},
                "work_cents": {"$sum": "$work_cents"},
                "work_records": {"$sum": "$work_records"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": ASCENDING}},
    ]
    return "employees", pipeline, "dept_joined"


def late_leaderboard_pipeline(
    month: str, limit: int, department: Optional[str]
) -> tuple[str, list[dict], str]:
    first, last = month_range(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        # logs whose employee no longer exists drop out here
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        # $rank is standard competition ranking: ties share a rank and the next rank
        # is skipped. The rank is assigned before `limit` is applied.
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return "attendance_logs", pipeline, "date_emp"


def department_trend_pipeline(
    department: str, day_from: date_cls, day_to: date_cls
) -> tuple[str, list[dict], str]:
    """One row per calendar day. Every department employee emits one "join event"
    (clamped into the range) plus one event per log, so the day series is built, gap
    filled and averaged entirely inside MongoDB."""
    first, last = day_from.isoformat(), day_to.isoformat()
    bound_lo = datetime(day_from.year, day_from.month, day_from.day)
    bound_hi = bound_lo + timedelta(days=(day_to - day_from).days + 1)  # $densify upper bound is exclusive

    log_event = {
        "day": "$$l.date",
        "joins": 0,
        "present_halves": {
            "$cond": [
                {"$in": ["$$l.status", list(PRESENCE_STATUSES)]},
                {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 1, 2]},
                0,
            ]
        },
        "late_count": {"$cond": [{"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}, 1, 0]},
    }
    join_event = {
        # clamped into [first, last] so the row always exists; employees who join after
        # `last` still emit a row but add nothing to the headcount (R9).
        "day": {"$min": [{"$max": ["$joined_on", first]}, last]},
        "joins": {"$cond": [{"$lte": ["$joined_on", last]}, 1, 0]},
        "present_halves": 0,
        "late_count": 0,
    }

    pipeline = [
        {"$match": {"department": department}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        {
            "$project": {
                "_id": 0,
                "events": {
                    "$concatArrays": [
                        [join_event],
                        {"$map": {"input": "$logs", "as": "l", "in": log_event}},
                    ]
                },
            }
        },
        {"$unwind": "$events"},
        {
            "$group": {
                "_id": "$events.day",
                "joins": {"$sum": "$events.joins"},
                "present_halves": {"$sum": "$events.present_halves"},
                "late_count": {"$sum": "$events.late_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "joins": 1,
                "present_halves": 1,
                "late_count": 1,
                "day": {
                    "$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}
                },
            }
        },
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [bound_lo, bound_hi]}}},
        {
            "$set": {
                "joins": {"$ifNull": ["$joins", 0]},
                "present_halves": {"$ifNull": ["$present_halves", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {"$sort": {"day": ASCENDING}},
        # R9: headcount on a day = everyone who had joined by then
        {
            "$setWindowFields": {
                "sortBy": {"day": ASCENDING},
                "output": {
                    "headcount": {"$sum": "$joins", "window": {"documents": ["unbounded", "current"]}}
                },
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$let": {
                        "vars": {"dw": {"$dayOfWeek": {"date": "$day", "timezone": "UTC"}}},
                        "in": {"$and": [{"$gte": ["$$dw", 2]}, {"$lte": ["$$dw", 6]}]},
                    }
                }
            }
        },
        # attendance_rate as an integer scaled by 10000, rounded half-up with exact
        # integer arithmetic: floor((halves * 10000 + headcount) / (2 * headcount)).
        {
            "$set": {
                "rate_scaled": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$present_halves", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        # 7-row window over the current and 6 preceding rows, nulls excluded
        {
            "$setWindowFields": {
                "sortBy": {"day": ASCENDING},
                "output": {
                    "window_sum": {"$sum": "$rate_scaled", "window": {"documents": [-6, "current"]}},
                    "window_count": {
                        "$sum": {"$cond": [{"$eq": ["$rate_scaled", None]}, 0, 1]},
                        "window": {"documents": [-6, "current"]},
                    },
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$present_halves", 2]},
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$eq": ["$rate_scaled", None]},
                        None,
                        {"$divide": ["$rate_scaled", 10000]},
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$window_count", 0]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {
                                                "$add": [
                                                    {"$multiply": ["$window_sum", 2]},
                                                    "$window_count",
                                                ]
                                            },
                                            {"$multiply": [2, "$window_count"]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
            }
        },
    ]
    return "employees", pipeline, "dept_joined"


# --------------------------------------------------------------------------- #
# 6. Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # the unique index, not a prior read, decides the race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    query = {"department": department} if department else {}
    total = employees.count_documents(query)
    cursor = (
        employees.find(query)
        .hint(hint("dept_emp" if department else "emp_code_unique"))
        .sort([("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_out(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes_for(punched_at, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # The unique (emp_code, date) index is what makes simultaneous punch-ins safe:
        # exactly one insert wins, every other one raises DuplicateKeyError.
        attendance_logs.insert_one(dict(doc))
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    punched_at = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # The most recent punch-in at or before this instant - this is what makes an
    # overnight shift close correctly on the next calendar day.
    record = attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
        hint=hint("emp_punch_in"),
    )
    if record is None:
        raise HTTPException(404, "no punch-in found for this employee")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    opened_at = record["punch_in"]
    if punched_at <= opened_at:
        raise invalid(["body", "punched_at"], "punch_out must be after punch_in")
    if punched_at - opened_at > MAX_SHIFT:
        raise invalid(["body", "punched_at"], "punch_out must be within 24 hours of punch_in")

    fields = derive_fields(record["status"], opened_at, punched_at, record["date"], emp)
    updated = attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},  # also matches a missing punch_out
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": fields["work_hours"],
                "overtime_minutes": fields["overtime_minutes"],
                "half_day": fields["half_day"],
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        # another request closed the same record in the meantime
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "path", "date")
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    supplied = body.model_fields_set
    for field in ("status", "punch_in", "punch_out"):
        if field in supplied and getattr(body, field) is None:
            raise invalid(["body", field], "must not be null")
    if not supplied & {"status", "punch_in", "punch_out"}:
        raise invalid(["body"], "supply at least one of status, punch_in, punch_out")

    record = attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "no attendance record for this date")

    status = body.status if "status" in supplied else record["status"]
    if status in NON_PRESENCE_STATUSES:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise invalid(["body"], f"{status} records cannot have punch times")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if "punch_in" in supplied else record.get("punch_in")
        new_out = from_ms(body.punch_out) if "punch_out" in supplied else record.get("punch_out")
        if new_in is None:
            raise invalid(["body", "punch_in"], f"{status} requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise invalid(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise invalid(["body", "punch_out"], "punch_out must be after punch_in")
            if new_out - new_in > MAX_SHIFT:
                raise invalid(["body", "punch_out"], "punch_out must be within 24 hours of punch_in")

    fields = derive_fields(status, new_in, new_out, date, emp)
    before = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": int(record.get("late_minutes") or 0),
        "overtime_minutes": int(record.get("overtime_minutes") or 0),
        "half_day": bool(record.get("half_day") or False),
    }
    changes = {
        field: {"from": before[field], "to": value}
        for field, value in fields.items()
        if before[field] != value
    }
    if not changes:
        raise invalid(["body"], "the request would not change anything")

    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Optimistic guard: the record must still look the way it did when we read it, so
    # two concurrent corrections cannot overwrite each other's derived values. The
    # loser gets a 409; $push means no history entry is ever lost.
    guard = {
        "emp_code": emp_code,
        "date": date,
        "status": before["status"],
        "punch_in": before["punch_in"],
        "punch_out": before["punch_out"],
        "$expr": {
            "$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(record.get("history") or [])]
        },
    }
    updated = attendance_logs.find_one_and_update(
        guard,
        {"$set": fields, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record changed while this correction was being applied")
    return attendance_out(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: StatusQuery = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    if date_from:
        parse_date(date_from, "query", "date_from")
    if date_to:
        parse_date(date_to, "query", "date_to")
    if date_from and date_to and date_from > date_to:
        raise invalid(["query", "date_from"], "date_from must not be after date_to")

    query, sort, index = attendance_list_query(emp_code, date_from, date_to, status)
    total = attendance_logs.count_documents(query)
    cursor = (
        attendance_logs.find(query)
        .hint(hint(index))
        .sort(sort)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_out(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=MONTH_RE)],
):
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    coll, pipeline, index = employee_monthly_pipeline(emp_code, month)
    rows = list(db[coll].aggregate(pipeline, hint=dict(hint(index))))
    row = rows[0] if rows else {}

    first, last = month_range(month)
    working_days = working_days_between(
        date_cls.fromisoformat(first), date_cls.fromisoformat(last), emp.get("joined_on")
    )
    present_days = (row.get("present_halves", 0) or 0) / 2
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(row.get("leave_days", 0) or 0),
        "late_count": int(row.get("late_count", 0) or 0),
        "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": (
            round_half_up(Decimal(str(present_days)) * 100 / Decimal(working_days), 4)
            if working_days
            else None
        ),
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: Annotated[str, Query(pattern=MONTH_RE)],
    department: Optional[str] = None,
):
    coll, pipeline, index = department_summary_pipeline(month, department)
    items = []
    for row in db[coll].aggregate(pipeline, hint=dict(hint(index))):
        records = int(row.get("work_records", 0) or 0)
        items.append(
            {
                "department": row["_id"],
                "headcount": int(row["headcount"]),
                "present_days": round_half_up((row.get("present_halves", 0) or 0) / 2, 2),
                "avg_work_hours": (
                    round_half_up(
                        Decimal(int(row.get("work_cents", 0) or 0)) / (Decimal(100) * records), 2
                    )
                    if records
                    else None
                ),
                "late_count": int(row.get("late_count", 0) or 0),
                "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
                "leave_count": int(row.get("leave_count", 0) or 0),
                "on_duty_count": int(row.get("on_duty_count", 0) or 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: Annotated[str, Query(pattern=MONTH_RE)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    coll, pipeline, index = late_leaderboard_pipeline(month, limit, department)
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row.get("name"),
            "department": row.get("department"),
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        }
        for row in db[coll].aggregate(pipeline, hint=dict(hint(index)))
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    date_from: Annotated[str, Query(alias="from")],
    date_to: Annotated[str, Query(alias="to")],
):
    day_from = parse_date(date_from, "query", "from")
    day_to = parse_date(date_to, "query", "to")
    if day_to < day_from:
        raise invalid(["query", "to"], "to must not be before from")
    if (day_to - day_from).days + 1 > MAX_TREND_DAYS:
        raise invalid(["query", "to"], f"the range must not exceed {MAX_TREND_DAYS} days")
    if employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")

    coll, pipeline, index = department_trend_pipeline(department, day_from, day_to)
    items = list(db[coll].aggregate(pipeline, hint=dict(hint(index))))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Annotated[
        Literal[
            "attendance_list",
            "employee_monthly",
            "department_summary",
            "late_leaderboard",
            "department_trend",
        ],
        Path(),
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: StatusQuery = None,
    date_range_from: Optional[str] = Query(None, alias="from"),
    date_range_to: Optional[str] = Query(None, alias="to"),
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    def need(value: Optional[str], name: str) -> str:
        if not value:
            raise invalid(["query", name], f"{name} is required to explain {endpoint}")
        return value

    if endpoint == "attendance_list":
        if date_from:
            parse_date(date_from, "query", "date_from")
        if date_to:
            parse_date(date_to, "query", "date_to")
        query, sort, index = attendance_list_query(emp_code, date_from, date_to, status)
        command = {
            "find": "attendance_logs",
            "filter": query,
            "sort": dict(sort),
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "hint": dict(hint(index)),
        }
        collection = "attendance_logs"
    else:
        if endpoint == "employee_monthly":
            collection, pipeline, index = employee_monthly_pipeline(
                need(emp_code, "emp_code"), need(month, "month")
            )
        elif endpoint == "department_summary":
            collection, pipeline, index = department_summary_pipeline(
                need(month, "month"), department
            )
        elif endpoint == "late_leaderboard":
            collection, pipeline, index = late_leaderboard_pipeline(
                need(month, "month"), limit, department
            )
        else:
            day_from = parse_date(need(date_range_from, "from"), "query", "from")
            day_to = parse_date(need(date_range_to, "to"), "query", "to")
            if day_to < day_from:
                raise invalid(["query", "to"], "to must not be before from")
            collection, pipeline, index = department_trend_pipeline(
                need(department, "department"), day_from, day_to
            )
        command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
            "hint": dict(hint(index)),
        }

    explain = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": collection, "explain": jsonable(explain)}
