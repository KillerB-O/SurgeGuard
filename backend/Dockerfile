FROM python:3.12-slim

WORKDIR /app

# Dependencies resolve from pyproject.toml so the image cannot drift from what
# CI installs. Runtime deps only -- the dev extra stays out of the image.
COPY backend/pyproject.toml /app/pyproject.toml
COPY backend/app /app/app
RUN pip install --no-cache-dir .

# Migrations are run as a separate step, not on startup, so a restarting
# container can never race another one into applying schema changes.
COPY backend/alembic.ini /app/alembic.ini
COPY backend/migrations /app/migrations

RUN useradd --create-home --uid 1000 surgeguard
USER surgeguard

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
