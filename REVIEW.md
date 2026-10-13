# REVIEW.md

Defects found in the starter `app/main.py`.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `health()` | Always 200, never checks MongoDB. | Stop MongoDB, call `/health`. | Ping MongoDB; 503 on failure. |
| 2 | `EmployeeIn` | No validation: bad `emp_code`, email, dates, shift times; equal shifts accepted. | POST `{"emp_code":"x","joined_on":"nope"}` returns 201. | Patterns/length limits from the contract, real date check, `shift_start != shift_end`. |
| 3 | `create_employee()` | Check-then-insert, no unique index: concurrent creates all succeed. | Fire parallel creates with one `emp_code`. | Unique index on `emp_code`; `DuplicateKeyError` -> 409. |
| 4 | `create_employee()` | `datetime.now()` is naive local time; response returns a datetime, not epoch ms. | Run under a non-UTC `TZ`; look at `created_at`. | Aware UTC timestamp, returned as epoch ms. |
| 5 | `list_employees()` | `skip = page * page_size` skips page 1. | Request `page=1`. | `(page - 1) * page_size`. |
| 6 | `list_employees()` | `total` counts the whole collection, ignoring `department`. | Filter by department, compare `total`. | `count_documents` with the same filter. |
| 7 | `list_employees()` | No bounds on `page`/`page_size` (page 0 gives a negative skip, i.e. 500). Not sorted by `emp_code`. | `page=0`, `page_size=100000`. | `ge=1`, `le=100`; sort by `emp_code`. |
| 8 | `punch_in()` | Unknown employee: `emp["shift_start"]` on `None` gives a 500. | Punch in with an unknown code. | Explicit 404. |
| 9 | `punch_in()` | `fromtimestamp()` uses server local time; `if body.punched_at` treats 0 as missing; no range check (seconds, floats, huge values accepted); no truncation to whole seconds. | Send seconds-looking or `...500.900` values. | Strict int in the epoch-ms range, UTC conversion, truncate to seconds. |
| 10 | `punch_in()` | `date = ts.date()` in server time, not IST, and ignores overnight shifts (R1). | Overnight employee punching in at 02:00 IST. | IST attendance date with the overnight rule. |
| 11 | `punch_in()` | `status` is a free string: `ABSENT`, `"xyz"` accepted. | POST `status: "LEAVE"`. | Literal `PRESENT/WFH/ON_DUTY`. |
| 12 | `compute_late_minutes()` | Shift start is anchored to the punch-in's own (naive/UTC) date, wrong for IST and overnight. Also compares *truncated minutes* `> 10`, so 09:40:01 gives 0 instead of 10 (R2). | Punch at 09:40:01 on a 09:30 shift; any overnight shift. | Build shift start from the attendance date in IST; compare seconds `> 600`, return floored minutes. |
| 13 | `compute_work_hours()` | `round()` is binary float / banker's rounding, not half-up (R4). | A duration that lands exactly on a half cent, e.g. 0.125 h. | `Decimal.quantize(ROUND_HALF_UP)`. |
| 14 | `compute_overtime()` | No 30-minute minimum (R3); overnight shift end not on the next day; naive/tz mixing; never called by any endpoint. | Punch out 10 minutes after shift end. | Shift end built in IST (next day if overnight); 0 below 30 minutes. |
| 15 | `punch_in()` | Check-then-insert race; no unique key. | Two simultaneous identical requests both get 201. | Unique `(emp_code, date)` index; insert directly, 409 on duplicate. |
| 16 | `punch_in()` / `list_attendance()` | Response leaks `id` (ObjectId string); BSON datetimes returned instead of epoch ms; `doc` mutated. | Inspect any response. | Explicit serializer with only contract fields. |
| 17 | `list_attendance()` | Loads every match into Python, sorts and slices there; sorts by `date` only (no `emp_code` tiebreak); `total` is `len(docs)`. Slow at 100k records. | 100k-record dataset; explain shows full scan. | DB-side sort/skip/limit, `count_documents`, supporting index. |
| 18 | `list_attendance()` | No validation of `date_from`/`date_to`/`status`/paging; `date_from > date_to` not rejected. | Bad dates or reversed range. | 422 for each. |
| 19 | module | `MongoClient` without `tz_aware=True` returns naive datetimes. | Compare stored vs returned instants. | `tz_aware=True`, convert at the API boundary. |
| 20 | module | No indexes at all. | `explain` shows `COLLSCAN`. | Idempotent index creation at startup. |
| 21 | module | Punch-out, regularization, analytics and explain endpoints missing. | 404s. | Implemented per the contract. |

## Looked at and not treated as defects
- Using `> 10` for the grace period is the right *strictness*; the defect (#12) is that it is applied to truncated minutes.
- `(punch_out - punch_in) / 3600` is the right formula; only the rounding (#13) is wrong.
- `status_code=201` on create and punch-in matches the contract.
- Reading `MONGO_URI`/`MONGO_DB` from the environment with `load_dotenv()` is fine: `load_dotenv` does not override real environment variables.
