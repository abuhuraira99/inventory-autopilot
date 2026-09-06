# =============================================================================
# Inventory Autopilot
# =============================================================================
# A two-stage build. The first stage compiles the wheels; the second copies only
# what is needed to run. The result carries no compiler, no build headers and no
# package index cache -- a smaller image is also a smaller attack surface.
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: build the dependencies
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Pinned by minor version. An unpinned base means the image that passed testing
# is not the image that runs.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# argon2-cffi and cryptography need a compiler unless a wheel is available for
# the platform. Installing the toolchain here rather than in the final image
# keeps roughly 300 MB out of what actually ships.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libffi-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


# -----------------------------------------------------------------------------
# Stage 2: the runtime
# -----------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="Inventory Autopilot" \
      org.opencontainers.image.description="Keeps Amazon stock quantities in step with a vendor feed. Quantity only - never prices." \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # zoneinfo needs a tz database for America/New_York to resolve. The tzdata
    # package in requirements.txt provides one, but setting this makes the
    # container's own clock unambiguous in logs.
    TZ=UTC

# curl for the healthcheck below. Nothing else is added: no shell utilities, no
# editor, no compiler.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# A non-root user. If the application is ever compromised, the attacker lands
# as a user who cannot write to /usr, cannot install packages and cannot read
# other users' files.
RUN groupadd --system --gid 1001 autopilot \
 && useradd --system --uid 1001 --gid autopilot --create-home autopilot

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Copied with ownership set, so no separate chown layer is needed.
COPY --chown=autopilot:autopilot app/          ./app/
COPY --chown=autopilot:autopilot migrations/   ./migrations/
COPY --chown=autopilot:autopilot scripts/      ./scripts/
COPY --chown=autopilot:autopilot alembic.ini pyproject.toml ./

# The data directory holds downloaded feed files, generated reports and
# catalogue snapshots. Mounted as a volume in docker-compose.yml so it survives
# a container rebuild -- the catalogue snapshots are what a
# restore-to-a-past-day depends on.
RUN mkdir -p /app/data/quarantine /app/data/reports /app/data/backups \
 && chown -R autopilot:autopilot /app/data

USER autopilot

EXPOSE 8000

# The dashboard is not published to the internet in the recommended deployment,
# so this healthcheck is for Docker's own restart logic rather than for a load
# balancer. It calls the unauthenticated /health endpoint, which reports only
# whether the service is up.
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# One worker, deliberately.
#
# The scheduler runs inside the web process, and exactly one process must own
# it. Two workers would mean two schedulers; the PostgreSQL advisory lock in
# app/db.py would stop them colliding, but half the scheduled fires would be
# wasted and the "next run" time on the dashboard would flip between two
# answers. This workload is one job at a time, and one worker serves a
# dashboard used by a handful of people comfortably.
#
# To scale the dashboard later, run extra containers with
# ENABLE_SCHEDULER=false.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--no-server-header"]
