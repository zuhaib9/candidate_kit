# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code`; unique `attendance_logs(emp_code,date)` (guarantees one record per day and serves monthly lookups); `(date desc, emp_code asc)` for the attendance list sort and date-range analytics; `(emp_code, punch_in desc)` for finding the record to punch out; `employees(department, joined_on)` and `joined_on` for headcount. I rejected a status-first index: status is optional, and the list is ordered by date.
2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one` with no prior check. The unique index lets exactly one insert win; the other raises `DuplicateKeyError`, returned as 409.
3. **Ties.** I rank with `$rank` over total late minutes, then keep `rank <= limit`. Tied employees share a rank, so all of them are returned even if that exceeds `limit`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and left-joins logs with `$lookup`, so people with no logs still appear. Headcount counts distinct `emp_code`s, not joined rows.
5. **100x data.** Maintain pre-aggregated daily per-department counters and use keyset pagination for deep pages.
