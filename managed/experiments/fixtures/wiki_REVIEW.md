# Service Patterns

## Auth Service

### Recurring patterns
- Always parameterize SQL queries — never f-string interpolation. (8x)
- Don't log raw passwords or tokens in `print()` or `logger.info()`. (5x)
- Use `bcrypt.checkpw` inside try/except, but **never** swallow the exception silently — log and reject. (3x)
- Set cookies with `httponly=True, secure=True, samesite='Lax'` minimum. (4x)
- `debug=True` is forbidden in production handlers. (6x)

## Database

### Recurring patterns
- Use context managers (`with sqlite3.connect(...) as conn:`) for connection lifecycle. (7x)
- Index `users.email` and `sessions.token` — both are queried on every request. (3x)

## Author Patterns

### test-author
- SQL injection risk via f-string interpolation (3x)
- PII/credential leak in debug print/log (4x)
- Missing input validation on JSON body (2x)
- `pass` in except blocks swallowing real errors (5x)
