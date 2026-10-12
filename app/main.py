"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, fallback to defaults)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, model_validator
import pymongo
from pymongo import MongoClient, ReturnDocument

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Create all required indexes idempotently at startup."""
    def safe_create_index(collection, keys, **kwargs):
        try:
            collection.create_index(keys, **kwargs)
        except pymongo.errors.OperationFailure as e:
            if getattr(e, "code", None) == 86:  # IndexKeySpecsConflict: reconcile existing index specs
                for idx in collection.list_indexes():
                    if idx.get("key") == dict(keys):
                        collection.drop_index(idx["name"])
                        collection.create_index(keys, **kwargs)
                        break
            else:
                raise

    safe_create_index(db.employees, [("emp_code", pymongo.ASCENDING)], unique=True)
    safe_create_index(db.employees, [("department", pymongo.ASCENDING), ("joined_on", pymongo.ASCENDING)])
    safe_create_index(db.employees, [("joined_on", pymongo.ASCENDING)])

    safe_create_index(
        db.attendance_logs,
        [("emp_code", pymongo.ASCENDING), ("date", pymongo.ASCENDING)],
        unique=True
    )
    safe_create_index(db.attendance_logs, [("emp_code", pymongo.ASCENDING), ("punch_in", pymongo.DESCENDING)])
    safe_create_index(db.attendance_logs, [("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)])
    safe_create_index(db.attendance_logs, [("date", pymongo.ASCENDING), ("late_minutes", pymongo.ASCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helper Utilities
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float | Decimal], places: int = 2) -> Optional[float]:
    """R8: Exact half-up rounding (eliminating banker's round-to-even bias)."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -places
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def truncate_to_seconds(ts_ms: int) -> int:
    """R1: Truncate epoch milliseconds to whole seconds before calculations."""
    return (ts_ms // 1000) * 1000


def ms_to_utc_dt(ts_ms: int) -> datetime:
    """Convert epoch milliseconds (truncated to whole seconds) to UTC datetime."""
    sec = truncate_to_seconds(ts_ms) // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Convert datetime to epoch milliseconds integer."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """Check if shift spans across midnight (shift_end <= shift_start)."""
    sh_s, sm_s = map(int, shift_start.split(":"))
    sh_e, sm_e = map(int, shift_end.split(":"))
    return (sh_e, sm_e) <= (sh_s, sm_s)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date in IST. For overnight shifts, punch-ins before
    shift_end belong to the previous calendar day's shift.
    """
    punch_ist = punch_in_utc.astimezone(IST)
    if is_overnight_shift(shift_start, shift_end):
        sh_e, sm_e = map(int, shift_end.split(":"))
        shift_end_t = time(sh_e, sm_e)
        if punch_ist.time() < shift_end_t:
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, record_date_str: str) -> int:
    """
    R2: 10m grace period. Late only if punch-in > 10:00 after shift_start;
    late_minutes = floor elapsed minutes since shift_start.
    """
    punch_ist = punch_in_utc.astimezone(IST)
    rec_date = date.fromisoformat(record_date_str)
    h, m = map(int, shift_start.split(":"))
    start_dt = datetime.combine(rec_date, time(h, m), tzinfo=IST)
    diff_sec = (punch_ist - start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: Elapsed hours rounded to 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    hours_dec = Decimal(str(diff_sec)) / Decimal("3600")
    return round_half_up(hours_dec, 2)


def compute_overtime(
    punch_out_utc: datetime,
    shift_end: str,
    record_date_str: str,
    shift_start: str
) -> int:
    """
    R3: Whole minutes from shift_end to punch_out if >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    punch_ist = punch_out_utc.astimezone(IST)
    rec_date = date.fromisoformat(record_date_str)
    sh_e, sm_e = map(int, shift_end.split(":"))
    if is_overnight_shift(shift_start, shift_end):
        end_date = rec_date + timedelta(days=1)
    else:
        end_date = rec_date
    end_dt = datetime.combine(end_date, time(sh_e, sm_e), tzinfo=IST)
    diff_sec = (punch_ist - end_dt).total_seconds()
    diff_min = int(diff_sec // 60)
    return diff_min if diff_min >= 30 else 0


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB attendance document into AttendanceRecord response model."""
    history_out = []
    for h in doc.get("history") or []:
        changes_out = {}
        for k, v in (h.get("changes") or {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                f_val = dt_to_epoch_ms(f_val) if isinstance(f_val, datetime) else f_val
                t_val = dt_to_epoch_ms(t_val) if isinstance(t_val, datetime) else t_val
            changes_out[k] = {"from": f_val, "to": t_val}
        history_out.append({
            "at": dt_to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": changes_out,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out,
    }


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize MongoDB employee document into Employee response model."""
    created_at = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(created_at) if isinstance(created_at, datetime) else created_at,
    }


# --------------------------------------------------------------------------- #
# Pydantic Request / Response Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_employee(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on must be a valid calendar date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


# --------------------------------------------------------------------------- #
# Shared Aggregation Pipeline Builders
# --------------------------------------------------------------------------- #
def get_month_range(month_str: str) -> Tuple[str, str, int]:
    """Return (first_day, last_day, days_in_month)."""
    year, month = map(int, month_str.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]
    first_day = f"{month_str}-01"
    last_day = f"{month_str}-{days_in_month:02d}"
    return first_day, last_day, days_in_month


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """R7: Mon-Fri working days between start_date and end_date inclusive."""
    start_d = date.fromisoformat(start_date_str)
    end_d = date.fromisoformat(end_date_str)
    if start_d > end_d:
        return 0
    cnt = 0
    cur = start_d
    while cur <= end_d:
        if cur.weekday() < 5:  # Mon=0, Fri=4
            cnt += 1
        cur += timedelta(days=1)
    return cnt


def build_employee_monthly_pipeline(emp_code: str, month: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Build aggregation pipeline for monthly employee attendance summary (R5–R7)."""
    first_day, last_day, _ = get_month_range(month)
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": first_day, "$lte": last_day}
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$project": {
                "is_mon_fri": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},  # Mongo $dayOfWeek: 2=Mon..6=Fri
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_mon_fri", "$is_presence"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": ["$is_leave", 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        }
    ]
    return "attendance_logs", pipeline


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """Build cross-collection pipeline starting from employees to preserve headcount (R9)."""
    first_day, last_day, _ = get_month_range(month)
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": last_day}}
    if department:
        match_emp["department"] = department

    pipeline = [
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
                                    {"$gte": ["$date", first_day]},
                                    {"$lte": ["$date", last_day]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "has_log": {"$cond": [{"$ifNull": ["$logs", False]}, True, False]},
                "status": "$logs.status",
                "work_hours": "$logs.work_hours",
                "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
                "half_day": {"$ifNull": ["$logs.half_day", False]},
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    "$has_log",
                                    {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_list": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    "$has_log",
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]}
                                ]
                            },
                            "$work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$has_log", {"$gt": ["$late_minutes", 0]}]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            "$has_log",
                            "$late_minutes",
                            0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$has_log", {"$eq": ["$status", "LEAVE"]}]},
                            1,
                            0
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$has_log", {"$eq": ["$status", "ON_DUTY"]}]},
                            1,
                            0
                        ]
                    }
                }
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$work_hours_list"}, 0]},
                        {"$avg": "$work_hours_list"},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"department": 1}}
    ]
    return "employees", pipeline


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    """Build pipeline ranking late employees using window functions with ties (1, 2, 2, 4)."""
    first_day, last_day, _ = get_month_range(month)
    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": first_day, "$lte": last_day},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee"
            }
        },
        {"$unwind": "$employee"}
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {}
                    }
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        }
    ])
    return "attendance_logs", pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Build pipeline for daily trend series using $densify gap-filling and moving average."""
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
    dept_emp_codes = [e["emp_code"] for e in dept_emps]
    from_dt_utc = datetime.combine(d_from, time.min, tzinfo=timezone.utc)
    to_dt_utc_next = datetime.combine(d_to + timedelta(days=1), time.min, tzinfo=timezone.utc)

    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]}
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                }
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
                "_id": 0
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt_utc, to_dt_utc_next]
                }
            }
        },
        {"$sort": {"date_dt": 1}},
        {
            "$project": {
                "date_dt": 1,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$dayOfWeek": "$date_dt"}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"cur_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$cur_date"]}
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 1}}
                ],
                "as": "active_emps"
            }
        },
        {
            "$project": {
                "date_dt": 1,
                "date": 1,
                "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]},
                "headcount": {"$size": "$active_emps"},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$project": {
                "date_dt": 1,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_dt": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }
        },
        {"$sort": {"date": 1}}
    ]
    return "attendance_logs", pipeline


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc

    try:
        db.employees.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", pymongo.ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = ms_to_utc_dt(body.punched_at)
    else:
        now_ts = int(datetime.now(timezone.utc).timestamp()) * 1000
        punch_in_utc = ms_to_utc_dt(now_ts)

    att_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except pymongo.errors.DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = ms_to_utc_dt(body.punched_at)
    else:
        now_ts = int(datetime.now(timezone.utc).timestamp()) * 1000
        punch_out_utc = ms_to_utc_dt(now_ts)

    # Find most recent open record whose punch_in is at or before punch_out
    open_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": punch_out_utc}},
        sort=[("punch_in", pymongo.DESCENDING)]
    )

    if not open_record:
        raise HTTPException(status_code=404, detail="No punch-in record found")

    if open_record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_utc = open_record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    if diff_sec <= 0 or diff_sec > 86400:
        raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in and within 24 hours")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = bool(work_hours < 4.50)
    overtime_min = compute_overtime(punch_out_utc, emp["shift_end"], open_record["date"], emp["shift_start"])

    # Atomic update ensuring punch_out is still None (concurrency safe)
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": open_record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was concurrently punched out")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        try:
            date.fromisoformat(date_from)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_from")
    if date_to:
        try:
            date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date_to")

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be <= date_to")

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
    cursor = db.attendance_logs.find(q).sort([("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date_param: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    try:
        date.fromisoformat(date_param)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date in path")

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_param})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    curr_status = record.get("status")
    curr_in = record.get("punch_in")
    if curr_in and curr_in.tzinfo is None:
        curr_in = curr_in.replace(tzinfo=timezone.utc)
    curr_out = record.get("punch_out")
    if curr_out and curr_out.tzinfo is None:
        curr_out = curr_out.replace(tzinfo=timezone.utc)
    curr_wh = record.get("work_hours")
    curr_late = record.get("late_minutes", 0)
    curr_ot = record.get("overtime_minutes", 0)
    curr_hd = record.get("half_day", False)
    curr_history = record.get("history") or []

    target_status = body.status if body.status is not None else curr_status

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="Cannot supply punch times when setting ABSENT or LEAVE")
        new_in = None
        new_out = None
        new_wh = None
        new_late = 0
        new_ot = 0
        new_hd = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_in = ms_to_utc_dt(body.punch_in)
        else:
            new_in = curr_in

        if new_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        # R1: punch_in must stay on record's attendance date
        computed_date = get_attendance_date(new_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date_param:
            raise HTTPException(status_code=422, detail="punch_in does not match the attendance date")

        if body.punch_out is not None:
            new_out = ms_to_utc_dt(body.punch_out)
        else:
            new_out = curr_out

        if new_out is not None:
            diff_sec = (new_out - new_in).total_seconds()
            if diff_sec <= 0 or diff_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and within 24 hours")
            new_wh = compute_work_hours(new_in, new_out)
            new_hd = bool(new_wh < 4.50)
            new_ot = compute_overtime(new_out, emp["shift_end"], date_param, emp["shift_start"])
        else:
            new_wh = None
            new_hd = False
            new_ot = 0

        new_late = compute_late_minutes(new_in, emp["shift_start"], date_param)

    # Track only fields that actually changed
    changes: Dict[str, Any] = {}
    if target_status != curr_status:
        changes["status"] = {"from": curr_status, "to": target_status}
    if new_in != curr_in:
        changes["punch_in"] = {"from": curr_in, "to": new_in}
    if new_out != curr_out:
        changes["punch_out"] = {"from": curr_out, "to": new_out}
    if new_wh != curr_wh:
        changes["work_hours"] = {"from": curr_wh, "to": new_wh}
    if new_late != curr_late:
        changes["late_minutes"] = {"from": curr_late, "to": new_late}
    if new_ot != curr_ot:
        changes["overtime_minutes"] = {"from": curr_ot, "to": new_ot}
    if new_hd != curr_hd:
        changes["half_day"] = {"from": curr_hd, "to": new_hd}

    if not changes:
        raise HTTPException(status_code=422, detail="No fields changed")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match current history length
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": {"$size": len(curr_history)}
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {
                "history": history_entry
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularization conflict")

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    first_day, last_day, _ = get_month_range(month)
    joined_on = emp["joined_on"]

    # R7: Working days prorated from joined_on for mid-month joiners
    eff_start = max(first_day, joined_on)
    working_days = count_working_days(eff_start, last_day)

    collection, pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db[collection].aggregate(pipeline))

    if res:
        agg = res[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late = int(agg["total_late_minutes"])
        total_ot = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up((present_days / working_days) * 100, 2)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    collection, pipeline = build_department_summary_pipeline(month, department)
    raw_items = list(db[collection].aggregate(pipeline))
    items = []
    for item in raw_items:
        items.append({
            "department": item["department"],
            "headcount": item["headcount"],
            "present_days": item["present_days"],
            "avg_work_hours": round_half_up(item["avg_work_hours"], 2) if item["avg_work_hours"] is not None else None,
            "late_count": item["late_count"],
            "total_late_minutes": item["total_late_minutes"],
            "leave_count": item["leave_count"],
            "on_duty_count": item["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    collection, pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db[collection].aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        d_from = date.fromisoformat(from_date)
        d_to = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid from or to date")

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to must be >= from")

    diff_days = (d_to - d_from).days
    if diff_days > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    dept_count = db.employees.count_documents({"department": department})
    if dept_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    collection, pipeline = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db[collection].aggregate(pipeline))
    items = []
    for r in raw_items:
        rate = round_half_up(r["attendance_rate"], 4) if r["attendance_rate"] is not None else None
        mavg = round_half_up(r["moving_avg_7d"], 4) if r["moving_avg_7d"] is not None else None
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": r["present_count"],
            "late_count": r["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": mavg,
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from:
            try:
                date.fromisoformat(date_from)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_from")
        if date_to:
            try:
                date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_to")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")

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
        explain_res = db.command("explain", {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_res}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        collection, pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_res = db.command("explain", {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {}
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain_res}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        collection, pipeline = build_department_summary_pipeline(month, department)
        explain_res = db.command("explain", {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {}
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain_res}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        collection, pipeline = build_late_leaderboard_pipeline(month, limit, department)
        explain_res = db.command("explain", {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {}
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain_res}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        try:
            d_from = date.fromisoformat(from_date)
            d_to = date.fromisoformat(to_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid from or to date")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="to must be >= from")
        collection, pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_res = db.command("explain", {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {}
        }, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain_res}

    raise HTTPException(status_code=422, detail="Invalid endpoint")
