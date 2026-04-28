# Project Profile

## Languages
- Python 3.11 (Flask backend)

## Architecture
- HTTP API: Flask app handlers in `auth/`
- Storage: SQLite at `/var/data/users.db`

## Applicable Security Checks
1, 2, 3 (PII), 7 (SQL injection), 11, 13 (auth), 14 (input validation), 18 (error leakage), 24, 25 (silent failures)

Skipped: 9, 10, 17 (no template engines, infrastructure-as-code, or distributed systems in this PR's scope).

## Test Locations
- `tests/auth/` — pytest, runs against in-memory sqlite.
