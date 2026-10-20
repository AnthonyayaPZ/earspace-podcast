FROM node:22-bookworm-slim AS javascript
FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 EARSPACE_DATA_DIR=/data \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home earspace \
    && mkdir -p /app /data && chown earspace:earspace /app /data
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
ARG ENABLE_TRANSCRIPTION=false
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$ENABLE_TRANSCRIPTION" = "true" ]; then \
      uv sync --locked --no-dev --no-install-project --extra transcription; \
    else uv sync --locked --no-dev --no-install-project; fi
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$ENABLE_TRANSCRIPTION" = "true" ]; then \
      uv sync --locked --no-dev --no-editable --extra transcription; \
    else uv sync --locked --no-dev --no-editable; fi
USER earspace
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
