FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN groupadd --system embedding && useradd --system --gid embedding --home-dir /app embedding
COPY embedding-service/pyproject.toml ./
COPY embedding-service/src ./src
RUN pip install --no-cache-dir .

USER embedding
EXPOSE 8001
CMD ["uvicorn", "policy_embeddings.app:app", "--host", "0.0.0.0", "--port", "8001"]
