FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir uv==0.12.12 && uv sync --frozen --no-dev
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

# Unprivileged runtime user; the image content stays read-only for it.
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin mia
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"]
# Release step: `alembic upgrade head` with the same image before starting the app.
# FORWARDED_ALLOW_IPS must name the TLS proxy, otherwise per-IP limits see one client.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers"]
