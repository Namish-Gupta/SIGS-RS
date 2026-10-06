# ─────────────────────────────────────────────────────────────────────────────
# SIGS-RS Multi-stage Dockerfile
#
# Stage 1 (builder): install Python dependencies
# Stage 2 (runtime): lean production image
#
# SRS REQ-QA-3: platform-agnostic Docker container.
# SRS §2.4.1.2: Ubuntu 22.04 LTS base.
# ─────────────────────────────────────────────────────────────────────────────

# ─── Stage 1: builder ────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy source and dependency spec and install to a prefix dir for clean copy to runtime
COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir --prefix=/install .

# ─── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL maintainer="Namish Gupta"
LABEL description="SIGS-RS — Secure IoT Gateway Simulation for Resource Sensing"
LABEL version="1.0.0"

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY src/ ./src/

# Copy TLS certificates if present (gitignored, mounted in prod)
# COPY certs/ ./certs/

# Create non-root user for security
RUN addgroup --system sigs && adduser --system --ingroup sigs sigs
USER sigs

# Expose application port (HTTP; HTTPS configured via Uvicorn args)
EXPOSE 8000

# Health check for Docker (K8s uses its own probes)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Default command — override in docker-compose.yml or K8s for HTTPS
CMD ["python", "-m", "uvicorn", "sigs_rs.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1"]
