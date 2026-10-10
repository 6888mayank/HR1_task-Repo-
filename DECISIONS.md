# DECISIONS.md

1. **Indexes.**
   I created:
   - `employees`: `{"emp_code": 1}` (unique; guarantees business key uniqueness and O(1) lookups), `{"department": 1, "joined_on": 1}`, and `{"joined_on": 1}` (enables covered index scans for department summary headcount).
   - `attendance_logs`: `{"emp_code": 1, "date": 1}` (unique; enforces one record per employee-day and race safety), `{"emp_code": 1, "punch_in": -1}` (fast retrieval of the latest open punch-in during punch-out), `{"date": -1, "emp_code": 1}` (efficient sort and range scans for `/attendance`), and `{"date": 1, "late_minutes": 1}` (powers leaderboard index scans).
   - *Rejected*: An index on `status` alone; low cardinality (5 values) yields poor selectivity and wastes write overhead.

2. **Punch-in race.**
   Both requests compute the attendance date and attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` atomically accepts the first insert and rejects the second with a `DuplicateKeyError`. The exception handler intercepts this and immediately returns HTTP 409 Conflict. This eliminates check-then-insert race windows without application-level locking.

3. **Ties.**
   The leaderboard uses MongoDB's `$setWindowFields` with `$rank`, providing standard competition ranking (e.g., 1, 2, 2, 4). Because the filter `rank <= limit` applies *after* ranking, all tied employees at the cutoff are included (even if total items exceed `limit`), sorted deterministically by `emp_code` ascending.

4. **Headcount.**
   The pipeline originates from `employees` (not `attendance_logs`), filtering employees where `joined_on <= month_end`. It joins attendance records via `$lookup` followed by `$unwind(preserveNullAndEmptyArrays=True)`. Grouping by department and sizing the `$addToSet` of `emp_code` guarantees employees with zero attendance records are fully counted.

5. **One thing you would change for 100x the data.**
   At 10M+ records, on-the-fly aggregations across large date ranges would increase p99 latency. I would pre-aggregate daily and monthly rollups using MongoDB Change Streams / Scheduled Triggers, and shard `attendance_logs` using a compound shard key `{emp_code: "hashed", date: 1}` to horizontally distribute write and query throughput.
