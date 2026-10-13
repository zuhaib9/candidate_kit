# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All code is in `app/main.py`.

```bash
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=attendance_db
uvicorn app.main:app --port 8000
```

Indexes are created unchanged at startup. Instants are stored as BSON UTC datetimes and exposed as epoch
milliseconds; dates and months are strings. Analytics read the stored derived fields (`work_hours`, `late_minutes`,
`overtime_minutes`, `half_day`) and never recompute them. Rounding is half-up using exact integer arithmetic.
