# Employee Attendance & Analytics API

A FastAPI and MongoDB service implementing punch-in/punch-out business rules, audit trail regularizations, and high-performance MongoDB aggregation analytics.

## Tech Stack
- **Python**: 3.10+ (tested on Python 3.13)
- **FastAPI**: 0.110+
- **MongoDB**: 6.0+ (requires `$densify` and `$setWindowFields`)
- **PyMongo**: 4.7+
- **Pydantic**: 2.6+

## Architecture & Project Structure
All application logic, validation schemas, database indexes, and aggregation pipelines are intentionally consolidated in a single file per assignment requirements:
```
├── app/
│   ├── __init__.py
│   └── main.py              # Entire API implementation (all 12 endpoints)
├── requirements.txt         # Pinned core dependencies
├── REVIEW.md                # Code review of starter defects and fixes
├── DECISIONS.md             # Key architectural and design decisions
└── README.md                # Setup instructions and overview
```

## Quick Start

### 1. Environment Setup
```bash
python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configuration
Create a local `.env` file (do not commit this):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(Or point `MONGO_URI` to a free MongoDB Atlas M0 cluster).*

### 3. Seed Sample Data (Optional)
```bash
python sample_seed.py
```

### 4. Run the API Server
```bash
uvicorn app.main:app --port 8000 --reload
```
Interactive API documentation will be available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Features & Implemented Endpoints
1. `GET /health`: Readiness probe with live MongoDB ping (returns 503 if unavailable).
2. `POST /employees`: Create employee with unique index enforcement on `emp_code`.
3. `GET /employees`: Paginated employee list sorted by `emp_code` ascending with accurate filtered totals.
4. `POST /attendance/punch-in`: Punch-in recording with IST timezone handling, overnight shifts (R1), late minute calculations (R2), and atomic concurrency protection.
5. `POST /attendance/punch-out`: Closes active shift, calculating work hours (R4 half-up), overtime (R3 threshold), and half-day status (R5) with race-safe atomic updates.
6. `GET /attendance`: High-performance cursor-level sorting (`date` DESC, `emp_code` ASC) and date-range validation (R10).
7. `PATCH /attendance/{emp_code}/{date}`: Manual regularization with optimistic concurrency locking and append-only audit history trail.
8. `GET /analytics/employees/{emp_code}/monthly`: Employee monthly aggregation computing working days from join date (R7), present days, and half-up attendance percentage.
9. `GET /analytics/departments/summary`: Department-level monthly summary guaranteeing headcount inclusion for employees with zero logs (R9).
10. `GET /analytics/leaderboard/late`: Late-comers leaderboard using MongoDB `$setWindowFields` and `$rank` for standard competition ranking with tie inclusion.
11. `GET /analytics/departments/{department}/trend`: Daily departmental attendance trend utilizing `$densify` for continuous date sequences and 7-day moving averages.
12. `GET /admin/explain/{endpoint}`: Returns execution statistics and query plans verifying `IXSCAN` index utilization.
