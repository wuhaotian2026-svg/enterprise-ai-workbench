FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        libreoffice-core-nogui \
        libreoffice-writer-nogui \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system policy && useradd --system --gid policy --home-dir /app policy
COPY backend/pyproject.toml ./
COPY backend/src ./src
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/scripts/seed_hr_demo.py ./scripts/seed_hr_demo.py
COPY scripts/seed_demo.py ./scripts/seed_demo.py
COPY scripts/run_evaluation.py ./scripts/run_evaluation.py
COPY sample-data/evaluation ./evaluation
RUN pip install --no-cache-dir .

RUN mkdir -p /app/uploads && chown -R policy:policy /app
USER policy
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn policy_api.production:app --host 0.0.0.0 --port 8000"]
