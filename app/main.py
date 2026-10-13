import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
attendance = db.attendance_logs
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def truncate_second(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def now_utc() -> datetime:
    return truncate_second(datetime.now(timezone.utc))


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(microsecond=0)


def dt_to_ms(dt: Optional[datetime]):
    if dt is None:
        return None
    return int(truncate_second(dt).timestamp() * 1000)


def validate_date(s: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise HTTPException(422, "invalid date")
    try:
        date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, "invalid date")
    return s


def validate_month(s: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s):
        raise HTTPException(422, "invalid month")
    return s


def month_bounds(month: str):
    validate_month(month)
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return start, end


def round_half_up(value: float, places: int) -> float:
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def parse_shift(s: str):
    h, m = map(int, s.split(":"))
    return h, m


def is_overnight(emp: dict) -> bool:
    return emp["shift_end"] <= emp["shift_start"]


def attendance_date_for(emp: dict, punch_in: datetime) -> str:
    local = truncate_second(punch_in).astimezone(IST)
    d = local.date()
    if is_overnight(emp):
        eh, em = parse_shift(emp["shift_end"])
        if (local.hour, local.minute) < (eh, em):
            d -= timedelta(days=1)
    return d.isoformat()


def shift_start_dt(emp: dict, attendance_date: str) -> datetime:
    h, m = parse_shift(emp["shift_start"])
    d = date.fromisoformat(attendance_date)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST).astimezone(timezone.utc)


def shift_end_dt(emp: dict, attendance_date: str) -> datetime:
    h, m = parse_shift(emp["shift_end"])
    d = date.fromisoformat(attendance_date)
    if is_overnight(emp):
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST).astimezone(timezone.utc)


def compute_late_minutes(punch_in: datetime, emp: dict, attendance_date: str) -> int:
    secs = int((truncate_second(punch_in) - shift_start_dt(emp, attendance_date)).total_seconds())
    # late only if strictly more than 10:00 after shift start (R2); value is floored whole minutes since shift start
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = (truncate_second(punch_out) - truncate_second(punch_in)).total_seconds() / 3600
    return round_half_up(hours, 2)


def compute_overtime(punch_out: datetime, emp: dict, attendance_date: str) -> int:
    diff = int((truncate_second(punch_out) - shift_end_dt(emp, attendance_date)).total_seconds() // 60)
    return diff if diff >= 30 else 0


def derived(emp: dict, attendance_date: str, punch_in, punch_out):
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp, attendance_date)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    work = compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": late,
        "work_hours": work,
        "overtime_minutes": compute_overtime(punch_out, emp, attendance_date),
        "half_day": work < 4.50,
    }


def serialize_history(history):
    out = []
    for h in history or []:
        changes = {}
        for k, v in h.get("changes", {}).items():
            changes[k] = {"from": dt_to_ms(v.get("from")) if isinstance(v.get("from"), datetime) else v.get("from"),
                          "to": dt_to_ms(v.get("to")) if isinstance(v.get("to"), datetime) else v.get("to")}
        out.append({"at": dt_to_ms(h["at"]), "by": h["by"], "reason": h["reason"], "changes": changes})
    return out


def attendance_response(doc: dict):
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")), "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
        "history": serialize_history(doc.get("history", [])),
    }


def employee_response(doc):
    return {"emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
            "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
            "joined_on": doc["joined_on"], "created_at": dt_to_ms(doc["created_at"])}


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def joined_date(cls, v):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v): raise ValueError("invalid date")
        try: date.fromisoformat(v)
        except ValueError: raise ValueError("invalid date")
        return v

    @field_validator("shift_end")
    @classmethod
    def shift_diff(cls, v, info):
        if "shift_start" in info.data and v == info.data["shift_start"]:
            raise ValueError("shift_start and shift_end must differ")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.on_event("startup")
def startup():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="department_joined")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_day")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    attendance.create_index([("date", ASCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp_asc")
    attendance.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_emp_punchin")


@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_response(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    docs = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_response(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date_for(emp, ts)
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, "late_minutes": compute_late_minutes(ts, emp, d),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    candidate = attendance.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not candidate:
        raise HTTPException(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= candidate["punch_in"]:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts > candidate["punch_in"] + timedelta(hours=24):
        raise HTTPException(422, "punch-out is more than 24 hours after punch-in")
    vals = derived(emp, candidate["date"], candidate["punch_in"], ts)
    updated = attendance.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **vals}},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_response(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                   status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if date_from: validate_date(date_from)
    if date_to: validate_date(date_to)
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not exceed date_to")
    if status and status not in STATUSES: raise HTTPException(422, "invalid status")
    q = {}
    if emp_code: q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to: q["date"]["$lte"] = date_to
    if status: q["status"] = status
    total = attendance.count_documents(q)
    docs = attendance.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_response(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    date_str = date
    validate_date(date_str)
    emp = employees.find_one({"emp_code": emp_code})
    if not emp: raise HTTPException(404, "employee not found")
    current = attendance.find_one({"emp_code": emp_code, "date": date_str})
    if not current: raise HTTPException(404, "attendance record not found")
    supplied = body.model_fields_set
    if "punch_in" in supplied and body.punch_in is None: raise HTTPException(422, "punch_in cannot be null")
    if "punch_out" in supplied and body.punch_out is None: raise HTTPException(422, "punch_out cannot be null")
    old_status = current["status"]
    old_pi, old_po = current.get("punch_in"), current.get("punch_out")
    new_status = body.status if "status" in supplied else old_status
    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied: raise HTTPException(422, "leave/absent cannot have punch times")
        new_pi = new_po = None
    else:
        new_pi = ms_to_dt(body.punch_in) if "punch_in" in supplied else old_pi
        new_po = ms_to_dt(body.punch_out) if "punch_out" in supplied else old_po
        if new_pi is None: raise HTTPException(422, "presence status requires punch_in")
        if attendance_date_for(emp, new_pi) != date_str: raise HTTPException(422, "punch_in is not on the record attendance date")
        if new_po is not None:
            if new_po <= new_pi: raise HTTPException(422, "punch_out must be after punch_in")
            if new_po > new_pi + timedelta(hours=24): raise HTTPException(422, "punch-out is more than 24 hours after punch-in")
    vals = derived(emp, date_str, new_pi, new_po)
    oldvals = {"status": old_status, "punch_in": old_pi, "punch_out": old_po,
               "work_hours": current.get("work_hours"), "late_minutes": current.get("late_minutes", 0),
               "overtime_minutes": current.get("overtime_minutes", 0), "half_day": current.get("half_day", False)}
    newvals = {"status": new_status, "punch_in": new_pi, "punch_out": new_po, **vals}
    changes = {}
    for k in oldvals:
        if oldvals[k] != newvals[k]: changes[k] = {"from": oldvals[k], "to": newvals[k]}
    if not changes: raise HTTPException(422, "request changes nothing")
    hist = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: the observed history length is part of the update predicate.
    old_hist_len = len(current.get("history", []))
    predicate = {"_id": current["_id"], "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, old_hist_len]}}
    updated = attendance.find_one_and_update(predicate, {"$set": newvals, "$push": {"history": hist}}, return_document=True)
    if updated is None: raise HTTPException(409, "record changed concurrently; retry")
    return attendance_response(updated)


def workdays_expr(start_field, end_field):
    return {"$let": {"vars": {"days": {"$dateDiff": {"startDate": start_field, "endDate": end_field, "unit": "day", "timezone": "+05:30"}}},
        "in": {"$size": {"$filter": {"input": {"$map": {"input": {"$range": [0, "$$days"]}, "as": "i",
            "in": {"$dateAdd": {"startDate": start_field, "unit": "day", "amount": "$$i", "timezone": "+05:30"}}}},
            "as": "d", "cond": {"$in": [{"$dayOfWeek": {"date": "$$d", "timezone": "+05:30"}}, [2,3,4,5,6]]}}}}}}


def half_up_expr(expr, places):
    """Half-up rounding of an already-averaged value. Only use on integer-scaled inputs (see callers):
    floor(x + 0.5) is exact there, whereas floor(x * 10**p + 0.5) on a float is not."""
    f = 10 ** places
    return {"$divide": [{"$floor": {"$add": [expr, 0.5]}}, f]}


def half_up_ratio_int(present_expr, den_expr, mult, places):
    """round_half_up(present / den * mult, places) * 10**places as an exact integer, using integer arithmetic.
    `present` is a multiple of 0.5, so present*2 is an integer and no float tie can be misrounded."""
    n = {"$multiply": [{"$multiply": [present_expr, 2]}, (mult * 10 ** places) // 2]}
    return {"$floor": {"$divide": [{"$add": [{"$multiply": [2, n]}, den_expr]}, {"$multiply": [2, den_expr]}]}}


def employee_month_pipeline(emp_code, month):
    start, end = month_bounds(month)
    start_s, end_s = start.isoformat(), end.isoformat()
    start_dt = datetime(start.year, start.month, start.day, tzinfo=IST).astimezone(timezone.utc)
    next_month = end + timedelta(days=1)
    next_dt = datetime(next_month.year, next_month.month, next_month.day, tzinfo=IST).astimezone(timezone.utc)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {"from": "attendance_logs", "let": {"e": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$e"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}}
        ], "as": "logs"}},
        {"$set": {"month_start": start_dt, "month_end_excl": next_dt}},
        {"$set": {"working_days_start": {"$cond": [
            {"$gte": ["$joined_on", next_month.isoformat()]}, next_dt,
            {"$cond": [{"$gt": ["$joined_on", start_s]}, {"$dateFromString": {"dateString": "$joined_on", "timezone": "+05:30"}}, start_dt]}
        ]}}},
        {"$set": {"working_days": workdays_expr("$working_days_start", "$month_end_excl")}},
        {"$set": {
            "present_days": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$and": [
                    {"$in": ["$$l.status", list(PRESENCE)]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date", "timezone": "+05:30"}}}, [2,3,4,5,6]]}
                ]}}},
                "as": "l",
                "in": {"$cond": [{"$eq": [{"$ifNull": ["$$l.half_day", False]}, True]}, 0.5, 1]}
            }}},
            "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes",0]},0]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes",0]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.overtime_minutes",0]}}}}
        }},
        {"$set": {"attendance_pct": {"$cond": [{"$gt": ["$working_days",0]}, {"$divide": [half_up_ratio_int("$present_days", "$working_days", 100, 2), 100]}, None]}}},
        {"$project": {"_id":0, "emp_code":1, "month":month, "working_days":1, "present_days":1, "leave_days":1,
            "late_count":1, "total_late_minutes":1, "total_overtime_minutes":1, "attendance_pct":1}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    validate_month(month)
    if not employees.find_one({"emp_code": emp_code}): raise HTTPException(404, "employee not found")
    rows = list(employees.aggregate(employee_month_pipeline(emp_code, month)))
    return rows[0]


def department_summary_pipeline(month, department=None):
    start, end = month_bounds(month); start_s, end_s = start.isoformat(), end.isoformat()
    match = {"joined_on": {"$lte": end_s}}
    if department is not None: match["department"] = department
    pipeline = [
        {"$match": match},
        {"$lookup": {"from":"attendance_logs", "let":{"e":"$emp_code"}, "pipeline":[{"$match":{"$expr":{"$and":[
            {"$eq":["$emp_code","$$e"]},{"$gte":["$date",start_s]},{"$lte":["$date",end_s]}
        ]}}}], "as":"logs"}},
        {"$unwind":{"path":"$logs","preserveNullAndEmptyArrays":True}},
        {"$group": {
            "_id": "$department",
            "emps": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE)]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "timezone": "+05:30"}}}, [2,3,4,5,6]]}
                ]},
                {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]},
                0
            ]}},
            # work_hours is a 2-dp value; average it as integer hundredths so the final half-up is exact
            "avg_work_hours": {"$avg": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]},
                {"$round": [{"$multiply": ["$logs.work_hours", 100]}, 0]}, None
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
        }},
        {"$project":{"_id":0,"department":"$_id","headcount":{"$size":"$emps"},"present_days":1,
            "avg_work_hours":half_up_expr("$avg_work_hours",2),"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}},
        {"$sort":{"department":1}}
    ]
    # $avg over no matching work-hour values is null; keep it null rather than turning it into 0.
    pipeline[-2]["$project"]["avg_work_hours"] = {"$cond":[{"$ne":["$avg_work_hours",None]},half_up_expr("$avg_work_hours",2),None]}
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    validate_month(month)
    return {"month":month, "items":list(employees.aggregate(department_summary_pipeline(month, department)))}


def leaderboard_pipeline(month, department=None, limit=10):
    start, end = month_bounds(month)
    p = [{"$match":{"date":{"$gte":start.isoformat(),"$lte":end.isoformat()}}},
         {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},
                     "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},
         {"$match":{"total_late_minutes":{"$gt":0}}},
         {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}},
         {"$unwind":"$employee"}]
    if department is not None: p.append({"$match":{"employee.department":department}})
    p += [{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
          {"$match":{"rank":{"$lte":limit}}},
          {"$sort":{"total_late_minutes":-1,"_id":1}},
          {"$project":{"_id":0,"rank":1,"emp_code":"$_id","name":"$employee.name","department":"$employee.department",
                       "total_late_minutes":1,"late_count":1}}]
    return p


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    validate_month(month)
    return {"month":month,"items":list(attendance.aggregate(leaderboard_pipeline(month, department, limit)))}


def trend_pipeline(department, start_s, end_s):
    start = date.fromisoformat(start_s); end = date.fromisoformat(end_s)
    start_dt = datetime(start.year,start.month,start.day,tzinfo=IST).astimezone(timezone.utc)
    end_excl = datetime((end+timedelta(days=1)).year,(end+timedelta(days=1)).month,(end+timedelta(days=1)).day,tzinfo=IST).astimezone(timezone.utc)
    return [
        {"$match":{"date":{"$gte":start_s,"$lte":end_s}}},
        {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"employee"}},
        {"$unwind":"$employee"},
        {"$match":{"employee.department":department}},
        {"$group":{"_id":{"$dateFromString":{"dateString":"$date","timezone":"+05:30"}},
            "present_count":{"$sum":{"$cond":[{"$in":["$status",list(PRESENCE)]},{"$cond":[{"$eq":[{"$ifNull":["$half_day",False]},True]},0.5,1]},0]}},
            "late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}
        }},
        {"$project":{"_id":0,"date":"$_id","present_count":1,"late_count":1}},
        {"$unionWith":{"pipeline":[{"$documents":[{"date":start_dt,"present_count":0,"late_count":0}]}]}},
        {"$group":{"_id":"$date","present_count":{"$sum":"$present_count"},"late_count":{"$sum":"$late_count"}}},
        {"$project":{"_id":0,"date":"$_id","present_count":1,"late_count":1}},
        {"$densify":{"field":"date","range":{"step":1,"unit":"day","bounds":[start_dt,end_excl]}}},
        {"$set":{"present_count":{"$ifNull":["$present_count",0]},"late_count":{"$ifNull":["$late_count",0]},
                  "date_str":{"$dateToString":{"date":"$date","format":"%Y-%m-%d","timezone":"+05:30"}}}},
        {"$lookup":{"from":"employees","let":{"d":"$date_str"},"pipeline":[{"$match":{"department":department}},
            {"$match":{"$expr":{"$lte":["$joined_on","$$d"]}}}],"as":"heads"}},
        {"$set":{"headcount":{"$size":"$heads"},"is_working_day":{"$in":[{"$dayOfWeek":{"date":"$date","timezone":"+05:30"}},[2,3,4,5,6]]}}},
        # rate in integer 1/10000ths (exact half-up); averaged as integers, then rounded half-up once more
        {"$set": {"rate_i": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_ratio_int("$present_count", "$headcount", 1, 4),
            None
        ]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$ne": ["$rate_i", None]}, {"$divide": ["$rate_i", 10000]}, None]}}},
        {"$setWindowFields":{"sortBy":{"date":1},"output":{"moving_raw":{"$avg":"$rate_i","window":{"documents":[-6,0]}}}}},
        {"$set":{"moving_avg_7d":{"$cond":[{"$ne":["$moving_raw",None]},half_up_expr("$moving_raw",4),None]}}},
        {"$project":{"_id":0,"date":"$date_str","is_working_day":1,"headcount":1,"present_count":1,"late_count":1,
                      "attendance_rate":1,"moving_avg_7d":1}},
        {"$sort":{"date":1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    validate_date(from_); validate_date(to)
    if to < from_: raise HTTPException(422,"to must not precede from")
    if (date.fromisoformat(to)-date.fromisoformat(from_)).days + 1 > 92: raise HTTPException(422,"range exceeds 92 days")
    if not employees.find_one({"department":department}): raise HTTPException(404,"unknown department")
    return {"department":department,"items":list(attendance.aggregate(trend_pipeline(department,from_,to)))}


def explain_find(query, sort, skip, limit):
    cmd={"find":"attendance_logs","filter":query,"sort":sort,"skip":skip,"limit":limit}
    return db.command("explain",cmd,verbosity="executionStats")


def explain_aggregate(collection, pipeline):
    return db.command("explain",{"aggregate":collection,"pipeline":pipeline,"cursor":{}},verbosity="executionStats")


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str, emp_code: Optional[str]=None, month: Optional[str]=None, department: Optional[str]=None,
            limit: int=Query(10,ge=1,le=50), date_from: Optional[str]=None, date_to: Optional[str]=None,
            status: Optional[str]=None, from_: Optional[str]=Query(None,alias="from"), to: Optional[str]=None,
            page: int=Query(1,ge=1), page_size: int=Query(20,ge=1,le=100)):
    try:
        if endpoint == "attendance_list":
            if date_from: validate_date(date_from)
            if date_to: validate_date(date_to)
            if date_from and date_to and date_from > date_to: raise HTTPException(422,"invalid date range")
            q={}
            if emp_code:q["emp_code"]=emp_code
            if date_from or date_to:
                q["date"]={}
                if date_from:q["date"]["$gte"]=date_from
                if date_to:q["date"]["$lte"]=date_to
            if status:
                if status not in STATUSES: raise HTTPException(422,"invalid status")
                q["status"]=status
            out=explain_find(q,{"date":-1,"emp_code":1},(page-1)*page_size,page_size)
        elif endpoint == "employee_monthly":
            if not emp_code or not month: raise HTTPException(422,"emp_code and month are required")
            validate_month(month); out=explain_aggregate("employees",employee_month_pipeline(emp_code,month))
        elif endpoint == "department_summary":
            if not month: raise HTTPException(422,"month is required")
            validate_month(month); out=explain_aggregate("employees",department_summary_pipeline(month,department))
        elif endpoint == "late_leaderboard":
            if not month: raise HTTPException(422,"month is required")
            validate_month(month); out=explain_aggregate("attendance_logs",leaderboard_pipeline(month,department,limit))
        elif endpoint == "department_trend":
            if not department or not from_ or not to: raise HTTPException(422,"department, from and to are required")
            validate_date(from_); validate_date(to)
            if to < from_ or (date.fromisoformat(to)-date.fromisoformat(from_)).days+1>92: raise HTTPException(422,"invalid range")
            out=explain_aggregate("attendance_logs",trend_pipeline(department,from_,to))
        else:
            raise HTTPException(422,"unknown endpoint")
        return json_util.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))
