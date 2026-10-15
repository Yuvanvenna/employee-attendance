"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (loaded from .env if present; system env vars take precedence)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client: MongoClient = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at application startup."""
    try:
        # Employees: Unique index on natural key emp_code
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index("department")
        db.employees.create_index("joined_on")

        # Attendance logs: Natural compound key (emp_code, date) must be unique
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        # Compound index for list_attendance sorting and date-range queries
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)])
        # Lookup index for punch-out resolution
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
        # Lookup index for employee monthly summaries
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("status", ASCENDING), ("date", ASCENDING)])
    except PyMongoError:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def half_up(val: float | Decimal, decimals: int = 2) -> float:
    """Half-up rounding (R8) using Decimal.quantize."""
    d = Decimal(str(val)) if not isinstance(val, Decimal) else val
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def ms_to_dt(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC datetime truncated to whole seconds (R1)."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert a UTC or naive datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    Compute attendance date (YYYY-MM-DD) in IST per R1.
    For overnight shifts (shift_end <= shift_start), a punch-in whose IST time
    is strictly earlier than shift_end belongs to the shift starting the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight and punch_in_ist.time() < time(end_h, end_m, 0):
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, attendance_date: str, shift_start: str) -> int:
    """
    R2: Late minutes.
    Grace period is 10:00. Punch-in is late only if strictly > 10 min after shift_start.
    When late, late_minutes = whole minutes (floor) since shift_start.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    d = date.fromisoformat(attendance_date)
    shift_start_ist = datetime.combine(d, time(start_h, start_m, 0), tzinfo=IST)

    diff_seconds = int((punch_in_ist - shift_start_ist).total_seconds())
    if diff_seconds > 600:
        return diff_seconds // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: Work hours = seconds / 3600, rounded half-up to 2 decimals."""
    diff_sec = Decimal(int((punch_out_utc - punch_in_utc).total_seconds()))
    hours = diff_sec / Decimal(3600)
    return half_up(hours, 2)


def compute_overtime(punch_out_utc: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    """
    R3: Overtime minutes.
    Floor minutes between shift_end and punch_out. Counted only when >= 30, otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    punch_out_ist = punch_out_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    d = date.fromisoformat(attendance_date)
    is_overnight = (end_h, end_m) <= (start_h, start_m)
    if is_overnight:
        d += timedelta(days=1)

    shift_end_ist = datetime.combine(d, time(end_h, end_m, 0), tzinfo=IST)
    diff_sec = int((punch_out_ist - shift_end_ist).total_seconds())
    if diff_sec >= 1800:
        return diff_sec // 60
    return 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def format_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format an employee document for API response."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def format_history_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Format an audit history entry for API response."""
    changes_api = {}
    for field, change in entry.get("changes", {}).items():
        if field in ("punch_in", "punch_out"):
            changes_api[field] = {
                "from": dt_to_ms(change.get("from")),
                "to": dt_to_ms(change.get("to")),
            }
        else:
            changes_api[field] = {
                "from": change.get("from"),
                "to": change.get("to"),
            }
    return {
        "at": dt_to_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes_api,
    }


def format_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Format an attendance log document for API response (no internal _id or id)."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [format_history_entry(h) for h in doc.get("history", [])],
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("shift_end")
    @classmethod
    def validate_shift_times(cls, v: str, info):
        shift_start = info.data.get("shift_start")
        if shift_start and v == shift_start:
            raise ValueError("shift_start must differ from shift_end")
        return v


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """Readiness probe: checks MongoDB ping, returns 503 on failure."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="Database unavailable") from e


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Create an employee. Enforces unique emp_code via database index."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List employees sorted by emp_code ascending."""
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [format_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """Record a punch-in and create attendance document."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = ms_to_dt(body.punched_at)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    record_date = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, record_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_record(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """Record a punch-out and close the most recent active record."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = ms_to_dt(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    # Find most recent record whose punch_in <= punch_out
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found for this punch-out")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    punch_in_dt = record["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if diff_sec <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
    if diff_sec > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_mins = compute_overtime(punch_out_dt, record["date"], emp["shift_start"], emp["shift_end"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,  # Concurrency guard
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    return format_attendance_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date descending, then emp_code ascending."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    items = [format_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date_str}", tags=["attendance"])
def regularize_attendance(emp_code: str, date_str: str, body: RegularizeRequest):
    """Regularize (manually correct) one day's record with audit history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    # Status rules validation
    if body.status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch times when setting ABSENT or LEAVE")

    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        target_punch_in = None
        target_punch_out = None
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            target_punch_in = ms_to_dt(body.punch_in)
        else:
            target_punch_in = rec.get("punch_in")
            if target_punch_in and target_punch_in.tzinfo is None:
                target_punch_in = target_punch_in.replace(tzinfo=timezone.utc)

        if target_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires punch_in")

        if body.punch_out is not None:
            target_punch_out = ms_to_dt(body.punch_out)
        else:
            target_punch_out = rec.get("punch_out")
            if target_punch_out and target_punch_out.tzinfo is None:
                target_punch_out = target_punch_out.replace(tzinfo=timezone.utc)

    # Validate punch_in stays on record attendance date (R1)
    if target_punch_in is not None:
        in_date = get_attendance_date(target_punch_in, emp["shift_start"], emp["shift_end"])
        if in_date != date_str:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in resolves to {in_date}, which does not match record date {date_str}",
            )

    # Validate punch_out order and 24h limit
    if target_punch_in is not None and target_punch_out is not None:
        diff_sec = (target_punch_out - target_punch_in).total_seconds()
        if diff_sec <= 0:
            raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
        if diff_sec > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    # Recompute derived values
    if target_status in ("ABSENT", "LEAVE"):
        target_late = 0
        target_work_hours = None
        target_overtime = 0
        target_half_day = False
    else:
        target_late = compute_late_minutes(target_punch_in, date_str, emp["shift_start"])
        if target_punch_out is not None:
            target_work_hours = compute_work_hours(target_punch_in, target_punch_out)
            target_overtime = compute_overtime(target_punch_out, date_str, emp["shift_start"], emp["shift_end"])
            target_half_day = compute_half_day(target_work_hours)
        else:
            target_work_hours = None
            target_overtime = 0
            target_half_day = False

    # Check for actual changes
    orig_in_ms = dt_to_ms(rec.get("punch_in"))
    target_in_ms = dt_to_ms(target_punch_in)
    orig_out_ms = dt_to_ms(rec.get("punch_out"))
    target_out_ms = dt_to_ms(target_punch_out)

    changes_db: Dict[str, Any] = {}
    if rec.get("status") != target_status:
        changes_db["status"] = {"from": rec.get("status"), "to": target_status}
    if orig_in_ms != target_in_ms:
        changes_db["punch_in"] = {"from": rec.get("punch_in"), "to": target_punch_in}
    if orig_out_ms != target_out_ms:
        changes_db["punch_out"] = {"from": rec.get("punch_out"), "to": target_punch_out}
    if rec.get("work_hours") != target_work_hours:
        changes_db["work_hours"] = {"from": rec.get("work_hours"), "to": target_work_hours}
    if rec.get("late_minutes", 0) != target_late:
        changes_db["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": target_late}
    if rec.get("overtime_minutes", 0) != target_overtime:
        changes_db["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": target_overtime}
    if rec.get("half_day", False) != target_half_day:
        changes_db["half_day"] = {"from": rec.get("half_day", False), "to": target_half_day}

    if not changes_db:
        raise HTTPException(status_code=422, detail="A request that changes nothing is rejected")

    history_entry_db = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db,
    }

    # Optimistic locking: check existing history length so concurrent edits don't overwrite
    current_hist_len = len(rec.get("history", []))
    res = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            f"history.{current_hist_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": target_punch_in,
                "punch_out": target_punch_out,
                "work_hours": target_work_hours,
                "late_minutes": target_late,
                "overtime_minutes": target_overtime,
                "half_day": target_half_day,
            },
            "$push": {"history": history_entry_db},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not res:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict")

    return format_attendance_record(res)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """Monthly attendance summary for one employee."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    joined = date.fromisoformat(emp["joined_on"])

    # R7: working days (Mon-Fri) counted from joined_on
    working_days = sum(
        1
        for day in range(1, num_days + 1)
        if date(year, m, day).weekday() < 5 and date(year, m, day) >= joined
    )

    pipeline = build_employee_monthly_pipeline(emp_code, month)
    agg_res = list(db.attendance_logs.aggregate(pipeline))

    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        pct = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal(100)
        attendance_pct = half_up(pct, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "combined_logs": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "logs": {
                    "$reduce": {
                        "input": "$combined_logs",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}},
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": ["$$l.half_day", 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {"$ne": ["$$l.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "l",
                            "in": "$$l.work_hours",
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$gt": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": "$logs.late_minutes"
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {"$eq": ["$$l.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month."""
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        avg_wh = r.get("avg_work_hours")
        if avg_wh is not None:
            avg_wh = half_up(avg_wh, 2)

        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": float(r.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })

    return {"month": month, "items": items}


def build_leaderboard_pipeline(month: str, limit: int, department: Optional[str] = None) -> List[Dict[str, Any]]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}}
                },
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
    ])
    return pipeline


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard for a month using standard competition ranking."""
    pipeline = build_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt_next = (datetime.fromisoformat(to_date) + timedelta(days=1)).replace(tzinfo=timezone.utc)

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "as_date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "as_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_next],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$as_date"}},
                "as_date": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$as_date"},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$project": {"joined_on": 1}},
                ],
                "as": "dept_emps",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]},
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"as_date": ASCENDING},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {"$sort": {"as_date": ASCENDING}},
    ]


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend for a department."""
    emp_count = db.employees.count_documents({"department": department})
    if emp_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    if d_to < d_from:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")
    if (d_to - d_from).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    results = list(db.attendance_logs.aggregate(pipeline))

    # Gap-filling fallback in case attendance_logs has 0 documents for densify to anchor on
    if len(results) < (d_to - d_from).days + 1:
        # Pre-fetch department employees for headcount calculation
        dept_emps = list(db.employees.find({"department": department}, {"joined_on": 1}))
        result_map = {r["date"]: r for r in results}
        filled_items = []
        cur_date = d_from
        while cur_date <= d_to:
            cur_str = cur_date.isoformat()
            if cur_str in result_map:
                row = result_map[cur_str]
            else:
                is_work = cur_date.weekday() < 5
                hc = sum(1 for e in dept_emps if e.get("joined_on", "") <= cur_str)
                att_rate = 0.0 if (is_work and hc > 0) else None
                row = {
                    "date": cur_str,
                    "is_working_day": is_work,
                    "headcount": hc,
                    "present_count": 0.0,
                    "late_count": 0,
                    "attendance_rate": att_rate,
                    "moving_avg_7d": None,
                }
            filled_items.append(row)

        # Calculate moving_avg_7d across all days in range
        for i, item in enumerate(filled_items):
            window = [filled_items[j]["attendance_rate"] for j in range(max(0, i - 6), i + 1)]
            non_null_rates = [r for r in window if r is not None]
            if non_null_rates:
                item["moving_avg_7d"] = float(sum(non_null_rates) / len(non_null_rates))
            else:
                item["moving_avg_7d"] = None
        results = filled_items

    items = []
    for r in results:
        rate = r.get("attendance_rate")
        m_avg = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r.get("present_count", 0.0)),
            "late_count": int(r.get("late_count", 0)),
            "attendance_rate": half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": half_up(m_avg, 4) if m_avg is not None else None,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Run MongoDB explain with executionStats for an endpoint's main query."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q)
            .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
            .skip(skip)
            .limit(page_size)
        )
        plan = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_department_summary_pipeline(month, department)
        plan = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_leaderboard_pipeline(month, limit, department)
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
        if d_to < d_from or (d_to - d_from).days > 92:
            raise HTTPException(status_code=422, detail="Invalid date range")

        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        plan = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}
