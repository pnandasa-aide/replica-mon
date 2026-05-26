#!/usr/bin/env bash
#
# ReplicaMon Container Helper Script
#
# Usage:
#   ./replica-mon.sh                  # Run CLI tools (compare.py, monitor.py)
#   ./replica-mon.sh --help
#   ./replica-mon.sh --continuous
#   ./replica-mon.sh python3 compare.py --source X --target Y
#
# For DASHBOARD BACKEND, use:
#   podman-compose up -d
#

set -e

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load environment variables from .env file
ENV_FILE="${SCRIPT_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

# Container configuration
IMAGE_NAME="localhost/replica-mon:latest"
CONTAINER_NAME="replica-mon-cli-${RANDOM}"

# Ensure directories exist and are writable
mkdir -p "${SCRIPT_DIR}/cache" "${SCRIPT_DIR}/metrics"
chmod 777 "${SCRIPT_DIR}/cache" "${SCRIPT_DIR}/metrics" 2>/dev/null || true

# Check if image exists
if ! podman images --format "{{.Repository}}:{{.Tag}}" | grep -q "^${IMAGE_NAME}$"; then
    echo "🔨 Building replica-mon image..."
    # Build context is the parent directory to allow accessing sibling projects
    podman build -t "$IMAGE_NAME" -f "${SCRIPT_DIR}/Containerfile" "${SCRIPT_DIR}/.."
    if [ $? -ne 0 ]; then
        echo "❌ Build failed!" >&2
        exit 1
    fi
    echo "✅ Build successful!"
else
    if [[ "$*" != *"--format json"* ]]; then
        echo "📦 Using existing image: $IMAGE_NAME"
    fi
fi

# Determine if we should override the entrypoint
ENTRYPOINT_ARGS=()
if [ "$1" = "python3" ]; then
    ENTRYPOINT_ARGS=("--entrypoint" "python3")
    shift
fi

# Run container for CLI tools
if [[ "$*" == *"--format json"* ]]; then
    podman run -i --rm --name "$CONTAINER_NAME" \
        -e AS400_USER="$AS400_USER" \
        -e AS400_PASSWORD="$AS400_PASSWORD" \
        -e MSSQL_USER="$MSSQL_USER" \
        -e MSSQL_PASSWORD="$MSSQL_PASSWORD" \
        -e MSSQL_ADMIN_USER="$MSSQL_ADMIN_USER" \
        -e MSSQL_ADMIN_PASSWORD="$MSSQL_ADMIN_PASSWORD" \
        -e GLUESYNC_HOST="$GLUESYNC_HOST" \
        -e GLUESYNC_ADMIN_USERNAME="$GLUESYNC_ADMIN_USERNAME" \
        -e GLUESYNC_ADMIN_PASSWORD="$GLUESYNC_ADMIN_PASSWORD" \
        -e QADMCLI_PATH="qadmcli" \
        -v "${SCRIPT_DIR}/cache:/app/replica-mon/cache:Z" \
        -v "${SCRIPT_DIR}/metrics:/app/replica-mon/metrics:Z" \
        -v "${SCRIPT_DIR}/../qadmcli/config:/app/qadmcli/config:Z" \
        "${ENTRYPOINT_ARGS[@]}" "$IMAGE_NAME" "$@"
else
    if [[ "$*" != *"--format json"* ]]; then
        echo "🚀 Running replica-mon CLI: $*"
    fi
    podman run -it --rm --name "$CONTAINER_NAME" \
        -e AS400_USER="$AS400_USER" \
        -e AS400_PASSWORD="$AS400_PASSWORD" \
        -e MSSQL_USER="$MSSQL_USER" \
        -e MSSQL_PASSWORD="$MSSQL_PASSWORD" \
        -e MSSQL_ADMIN_USER="$MSSQL_ADMIN_USER" \
        -e MSSQL_ADMIN_PASSWORD="$MSSQL_ADMIN_PASSWORD" \
        -e GLUESYNC_HOST="$GLUESYNC_HOST" \
        -e GLUESYNC_ADMIN_USERNAME="$GLUESYNC_ADMIN_USERNAME" \
        -e GLUESYNC_ADMIN_PASSWORD="$GLUESYNC_ADMIN_PASSWORD" \
        -e QADMCLI_PATH="qadmcli" \
        -v "${SCRIPT_DIR}/cache:/app/replica-mon/cache:Z" \
        -v "${SCRIPT_DIR}/metrics:/app/replica-mon/metrics:Z" \
        -v "${SCRIPT_DIR}/../qadmcli/config:/app/qadmcli/config:Z" \
        "${ENTRYPOINT_ARGS[@]}" "$IMAGE_NAME" "$@"
fi
