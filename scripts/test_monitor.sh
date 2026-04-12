#!/bin/bash
#
# Test Monitor - Quick wrapper script
# Generate mockup data and verify replication
#
# Usage:
#   ./test_monitor.sh
#   ./test_monitor.sh --tables CUSTOMERS,CUSTOMERS2
#   ./test_monitor.sh --rows 20 --wait 180
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║        Replication Test Monitor                          ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# Run the Python test monitor
python3 "$SCRIPT_DIR/test_monitor.py" "$@"

exit_code=$?

if [ $exit_code -eq 0 ]; then
    echo ""
    echo "✅ All tests completed successfully!"
else
    echo ""
    echo "⚠️  Some tests failed. Check the output above."
fi

exit $exit_code
