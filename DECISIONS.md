# DECISIONS.md

1. **Indexes.**
   - `employees`: `emp_code` (unique: enforces business key uniqueness and accelerates lookups), `department` (filters), and `joined_on` (headcount cutoff).
   - `attendance_logs`: `(emp_code, date)` (unique: guarantees at most one record per employee per attendance day), `(date, emp_code)` (serves attendance listing sort and range queries), `(emp_code, punch_in)` (punch-out lookup), and `(emp_code, status, date)` (monthly summaries).
   - *Rejected:* Standalone index on `attendance_logs.punch_out`. It incurs index maintenance write overhead with negligible benefit, since lookups are already scoped by `emp_code` and `punch_in`.

2. **Punch-in race.**
   Both requests concurrently resolve the employee and compute the IST attendance date. When both attempt `insert_one()`, MongoDB's unique compound index on `(emp_code, date)` atomically accepts the first insert (returning 201 Created) and rejects the second with `DuplicateKeyError`, which our exception handler translates into an HTTP 409 Conflict.

3. **Ties.**
   The leaderboard uses MongoDB's `$rank` window function for standard competition ranking (e.g., 1, 2, 2, 4). The `$match` filters `rank <= limit`. If a tie occurs at the cutoff, all tied employees are returned (yielding more than `limit` rows), ordered by `total_late_minutes` descending and `emp_code` ascending.

4. **Headcount.**
   The aggregation pipeline starts from the `employees` collection (filtering `joined_on <= end_of_period`) and uses `$lookup` to bring in attendance logs. This guarantees every active employee is counted in headcount, even if they have zero logs.

5. **One thing you would change.**
   For 100x data, I would maintain pre-aggregated daily and monthly attendance rollups via MongoDB change streams or background workers rather than computing multi-collection aggregations over raw punch logs on demand.
