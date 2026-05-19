#!/usr/bin/env bash
#
# ReplicaMon Container Helper Script for Bash
#
# Usage:
#   ./replica-mon.sh
#   ./replica-mon.sh --help
#   ./replica-mon.sh --continuous
#
# To run compare.py:
#   ./replica-mon.sh python3 compare.py --source X --target Y
#

set -e

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load environment variables from .env file
ENV_FILE="${SCRIPT_DIR}/../.env"
if [ -f "$ENV_FILE" ]; then
    export $(grep -v '^#' "$ENV_FILE" | xargs)
fi

# Container configuration
IMAGE_NAME="replica-mon"
CONTAINER_NAME="replica-mon-${RANDOM}"

# Ensure cache directory exists and is writable
mkdir -p "${SCRIPT_DIR}/cache"
chmod 777 "${SCRIPT_DIR}/cache"

# Check for --rebuild flag
FORCE_REBUILD=false
if [ "$1" = "--rebuild" ]; then
    FORCE_REBUILD=true
    shift # Remove --rebuild from args
fi

# Check if image exists (use sudo to match root podman storage)
if [ "$FORCE_REBUILD" = true ] || ! sudo podman images --format "{{.Repository}}" | grep -q "^localhost/${IMAGE_NAME}$"; then
    echo "🔨 Building replica-mon image..."
    # IMPORTANT: Build context is the parent directory to allow accessing qadmcli
    sudo podman build -t "$IMAGE_NAME" -f "${SCRIPT_DIR}/Containerfile" "${SCRIPT_DIR}/.."
    if [ $? -ne 0 ]; then
        echo "❌ Build failed!" >&2
        exit 1
    fi
    echo "✅ Build successful!"
else
    # Only show if not in json format
    if [[ "$*" != *"--format json"* ]]; then
        echo "📦 Using existing image: $IMAGE_NAME"
    fi
fi

# Determine if we should override the entrypoint (e.g., if user calls `python3 compare.py`)
ENTRYPOINT_ARGS=()
if [ "$1" = "python3" ]; then
    ENTRYPOINT_ARGS=("--entrypoint" "python3")
    shift # Remove python3 from args
fi

# Run container (use sudo for root podman)
if [[ "$*" == *"--format json"* ]]; then
    sudo podman run -i --rm --name "$CONTAINER_NAME" \
        -e AS400_USER="$AS400_USER" \
        -e AS400_PASSWORD="$AS400_PASSWORD" \
        -e MSSQL_USER="$MSSQL_USER" \
        -e MSSQL_PASSWORD="$MSSQL_PASSWORD" \
        -e MSSQL_ADMIN_USER="$MSSQL_ADMIN_USER" \
        -e MSSQL_ADMIN_PASSWORD="$MSSQL_ADMIN_PASSWORD" \
        -e QADMCLI_PATH="qadmcli" \
        -v "${SCRIPT_DIR}/cache:/app/cache:Z" \
        "${ENTRYPOINT_ARGS[@]}" "$IMAGE_NAME" "$@"
else
    # Only print "Running..." if we aren't executing a json output format
    echo "🚀 Running replica-mon $*"
    sudo podman run -it --rm --name "$CONTAINER_NAME" \
        -e AS400_USER="$AS400_USER" \
        -e AS400_PASSWORD="$AS400_PASSWORD" \
        -e MSSQL_USER="$MSSQL_USER" \
        -e MSSQL_PASSWORD="$MSSQL_PASSWORD" \
        -e MSSQL_ADMIN_USER="$MSSQL_ADMIN_USER" \
        -e MSSQL_ADMIN_PASSWORD="$MSSQL_ADMIN_PASSWORD" \
        -e QADMCLI_PATH="qadmcli" \
        -v "${SCRIPT_DIR}/cache:/app/cache:Z" \
        "${ENTRYPOINT_ARGS[@]}" "$IMAGE_NAME" "$@"
fi
