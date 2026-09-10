# Database migrations

Alembic uses the process `MIA_TEST_DATABASE_URL` for isolated integration tests,
then `DATABASE_URL`, and otherwise the documented local PostgreSQL URL in
`alembic.ini`. It never loads a `.env` file.

Run `alembic upgrade head`; rollback verification uses `alembic downgrade base`.
