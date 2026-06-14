# ReplicaMon - Replication Monitoring & Reconciliation
# Containerfile for Podman/Docker

FROM python:3.11-slim-bookworm

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    JT400_JAR=/opt/jt400/jt400.jar

# Install system dependencies including ODBC for MSSQL
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    unixodbc-dev \
    openjdk-17-jre-headless \
    curl \
    ca-certificates \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Install Microsoft ODBC Driver for SQL Server
RUN curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg && \
    curl https://packages.microsoft.com/config/debian/12/prod.list | tee /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y msodbcsql18 && \
    rm -rf /var/lib/apt/lists/*

# Download jt400.jar if needed (we rely on qadmcli structure)
RUN mkdir -p /opt/jt400

# Install qadmcli dependency
COPY qadmcli /opt/qadmcli
# Copy jt400 from qadmcli if it exists there
RUN cp /opt/qadmcli/lib/jt400.jar /opt/jt400/jt400.jar || echo "jt400.jar not found in qadmcli"

RUN pip install --upgrade pip && \
    cd /opt/qadmcli && pip install .[agent] && \
    pip install fastapi uvicorn websockets requests pydantic aiofiles httpx oracledb

# Create app directory
WORKDIR /app

# Copy application code and sdk
COPY replica-mon /app/replica-mon
COPY replica_msdk /app/replica_msdk
COPY replica-cli /app/replica-cli
# Copy qadmcli config (connection.yaml) so backend can discover AS400 connection
COPY qadmcli/config /app/qadmcli/config

# Also set PYTHONPATH so imports work
ENV PYTHONPATH="/app"

# Make sure required directories exist with right permissions
RUN mkdir -p /app/cache /app/replica-mon/metrics /app/replica-mon/cache

# Note: rootless Podman already provides security isolation — container root maps to
# the host's unprivileged user (ubuntu), so no extra USER directive needed.
# Adding a non-root user breaks host volume mounts due to UID mapping in rootless mode.

# Set default entrypoint to FastAPI
# Use --app-dir to avoid Python import issues with hyphenated directory names
ENTRYPOINT ["uvicorn", "main:app", "--app-dir", "/app/replica-mon/backend", "--host", "0.0.0.0", "--port", "8000"]
