#!/bin/bash

echo "============================================================"
echo "    🚀 Starting End-to-End Monitor Engine Test 🚀"
echo "============================================================"

# Pre-authenticate sudo to prevent qadmcli from hanging
echo "Please enter your sudo password if prompted (needed for qadmcli):"
sudo -v

START_TIME=$(date +"%Y-%m-%d %H:%M:%S")

echo ""
echo "[Step 1] Starting monitor engine in the background..."
python3 monitor.py &
MONITOR_PID=$!
sleep 5 # Let monitor initialize and generate entities.json

echo ""
echo "[Step 2 & 3] Retrieving entities & generating traffic..."
python3 generate_test_traffic.py all 20

echo ""
echo "[Step 4] Waiting 15 seconds to ensure replication propagates..."
sleep 15

echo ""
echo "[Step 5] Stopping monitor engine..."
kill $MONITOR_PID 2>/dev/null

echo ""
echo "[Step 6] Running comparison tool to measure results..."
echo "============================================================"

if [ -f entities.json ]; then
    PIPELINE=$(python3 -c "import json; print(json.load(open('entities.json'))['pipeline'])")
    
    # Loop through each entity and run compare
    python3 -c "import json; [print(e['entityId']) for e in json.load(open('entities.json'))['entities']]" | while read ENTITY_ID; do
        echo "📊 Comparing Entity ID: $ENTITY_ID"
        python3 cli.py compare --pipeline "$PIPELINE" --entity "$ENTITY_ID" --since "$START_TIME"
        echo "------------------------------------------------------------"
    done
else
    echo "❌ Error: entities.json not found! Cannot run comparison."
fi

echo "============================================================"
echo "Test execution complete."
