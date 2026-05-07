# ---- Builder stage ----
FROM python:3.11-slim AS builder

WORKDIR /build

ARG EXTRAS="documents,scheduler,system_control,vector"

COPY pyproject.toml README.md /build/
COPY app /build/app
COPY prompts /build/prompts

RUN pip install --no-cache-dir --prefix=/install ".[${EXTRAS}]"

# ---- Runtime stage ----
FROM python:3.11-slim

LABEL org.opencontainers.image.title="KERN AI Workspace" \
      org.opencontainers.image.description="Privacy-first local AI workspace for German enterprises" \
      org.opencontainers.image.vendor="KERN"

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY pyproject.toml README.md /app/
COPY app /app/app
COPY prompts /app/prompts
COPY .env.example /app/.env.example

# Copy and prepare entrypoint
COPY scripts/docker-entrypoint.sh /app/scripts/docker-entrypoint.sh
RUN chmod +x /app/scripts/docker-entrypoint.sh

# Runtime dependencies (curl for health checks, PortAudio for sounddevice import)
RUN apt-get update && apt-get install -y --no-install-recommends curl libportaudio2 && \
    rm -rf /var/lib/apt/lists/*

# Install the app itself (lightweight, deps already installed)
RUN pip install --no-cache-dir --no-deps .

# Dependency audit and lock file
RUN python -m pip install --no-cache-dir --upgrade pip wheel
RUN pip install --no-cache-dir pip-audit && pip-audit
RUN pip freeze > /app/requirements.lock

# Create data directories
RUN mkdir -p /data/profiles /data/backups /data/documents /data/attachments

# Environment defaults for containerized deployment
ENV KERN_DB_PATH=/data/kern.db \
    KERN_SYSTEM_DB_PATH=/data/kern-system.db \
    KERN_ROOT_PATH=/data \
    KERN_PROFILE_ROOT=/data/profiles \
    KERN_BACKUP_ROOT=/data/backups \
    KERN_DOCUMENT_ROOT=/data/documents \
    KERN_ATTACHMENT_ROOT=/data/attachments \
    KERN_PRODUCT_POSTURE=production \
    KERN_POLICY_MODE=corporate

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f -m 5 http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
