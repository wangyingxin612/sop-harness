# syntax=docker/dockerfile:1

# ---- stage 1: build the frontend -------------------------------------------------
FROM node:20-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# ---- stage 2: backend runtime -----------------------------------------------------
FROM python:3.12-slim AS runtime
WORKDIR /app

# System deps kept minimal — no compiler toolchain needed for these pure-Python deps.
RUN pip install --no-cache-dir --upgrade pip

COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/app /app/backend/app
RUN pip install --no-cache-dir /app/backend

COPY backend/sops /app/backend/sops
COPY backend/fixtures /app/backend/fixtures

# Built frontend, served by FastAPI's StaticFiles mount (app/api/main.py)
COPY --from=frontend-build /frontend/dist /app/frontend/dist

WORKDIR /app/backend
ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# No hardcoded API key: ANTHROPIC_API_KEY is passed at `docker run` time
# (see README.md), or a per-session key can be entered in the UI (R10 asks
# for both delivery paths, so both are supported).
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
