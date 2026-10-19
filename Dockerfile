FROM node:22-bookworm-slim AS javascript
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 EARSPACE_DATA_DIR=/data
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home earspace \
    && mkdir -p /app /data && chown earspace:earspace /app /data
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY pyproject.toml README.md requirements.lock ./
COPY app ./app
ARG ENABLE_TRANSCRIPTION=false
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . \
    && if [ "$ENABLE_TRANSCRIPTION" = "true" ]; then pip install --no-cache-dir '.[transcription]'; fi
USER earspace
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
