FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN groupadd --gid 10001 appgroup && \
    useradd --uid 10001 --gid appgroup --shell /bin/bash --create-home appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY resolutions_agent/ ./resolutions_agent/
COPY app_server/ ./app_server/
COPY evals/ ./evals/
COPY pyproject.toml README.md ./

RUN chown -R appuser:appgroup /app
USER appuser

EXPOSE 8080

CMD ["uvicorn", "app_server.main:fastapi_app", "--host", "0.0.0.0", "--port", "8080"]
