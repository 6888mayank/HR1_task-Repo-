# Employee Attendance & Analytics API

A high-performance Employee Attendance and Analytics backend service built with Python, FastAPI, and MongoDB.

## Features
- **Accurate Business Rules (R1–R10)**: IST timezone resolution, overnight shift handling, 10-minute grace period with full late minutes calculation, half-up rounding (`ROUND_HALF_UP`) for work hours, overtime calculation with 30-minute threshold, and mid-month joiner working day prorating.
- **Race-Safe Operations**: Guaranteed concurrency safety for duplicate punch-ins, simultaneous punch-outs, and optimistic concurrency history tracking on manual regularizations.
- **Advanced MongoDB Aggregation Pipelines**: Pure database-side analytics utilizing cross-collection lookups, window functions (`$setWindowFields`, `$rank`), and date gap filling (`$densify`) with zero Python loops.
- **Index Optimization**: All primary queries and aggregations are backed by compound indexes created idempotently at startup, verified to use `IXSCAN` with zero `COLLSCAN`.

## Requirements
- Python 3.11+
- MongoDB 6.0+ (Tested against MongoDB 7.0)

## Getting Started

1. **Clone the repository:**
   ```bash
   git clone https://github.com/6888mayank/HR1_task-Repo-.git
   cd HR1_task-Repo-
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables:**
   Set `MONGO_URI` and `MONGO_DB` (or rely on the defaults `mongodb://localhost:27017` and `attendance_db`):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

4. **Seed Sample Data (Optional):**
   ```bash
   python sample_seed.py
   ```

5. **Run the API server:**
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

## API Documentation
Interactive OpenAPI documentation is available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Structure
- [app/main.py](app/main.py): Complete API implementation, models, aggregations, and startup index initialization.
- [REVIEW.md](REVIEW.md): Code review findings and defect resolutions from the starter implementation.
- [DECISIONS.md](DECISIONS.md): Architectural decisions covering index design, concurrency, tie handling, headcount tracking, and 100x scaling strategy.
