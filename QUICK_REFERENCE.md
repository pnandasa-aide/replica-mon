# GlueSync Quick Reference - Essential Commands

## Environment Setup

```bash
cd ~/_qoder
source .env
export AS400_USER AS400_PASSWORD MSSQL_USER MSSQL_PASSWORD MSSQL_ADMIN_USER MSSQL_ADMIN_PASSWORD
export GLUESYNC_ADMIN_USERNAME GLUESYNC_ADMIN_PASSWORD
```

---

## 1. Create Source Table on AS400 with Journaling

```bash
cd ~/_qoder/qadmcli

# Create table
./qadmcli.sh as400 execute \
    -q "CREATE TABLE GSLIBTST.CUSTOMERS (
        CUST_ID BIGINT NOT NULL PRIMARY KEY,
        FIRST_NAME VARCHAR(50),
        LAST_NAME VARCHAR(50),
        EMAIL VARCHAR(100),
        CREATED_DATE TIMESTAMP
    )"

# Enable journaling
./qadmcli.sh journal enable -n CUSTOMERS -l GSLIBTST

# Verify journaling
./qadmcli.sh journal info -n CUSTOMERS -l GSLIBTST
```

---

## 2. Create Target Table on MSSQL with Change Tracking

```bash
cd ~/_qoder/qadmcli

# Create table
./qadmcli.sh mssql execute \
    -q "CREATE TABLE dbo.CUSTOMERS (
        CUST_ID BIGINT PRIMARY KEY,
        FIRST_NAME NVARCHAR(50),
        LAST_NAME NVARCHAR(50),
        EMAIL NVARCHAR(100),
        CREATED_DATE DATETIME2
    )"

# Enable CT on database (once per database)
./qadmcli.sh mssql ct enable-db \
    --admin-user $MSSQL_ADMIN_USER \
    --admin-password $MSSQL_ADMIN_PASSWORD \
    --retention 2

# Enable CT on table
./qadmcli.sh mssql ct enable-table -t CUSTOMERS -s dbo

# Verify CT status
./qadmcli.sh mssql ct status -t CUSTOMERS -s dbo
```

---

## 3. Create Pipeline and Add Entity

```bash
cd ~/_qoder/gluesync-cli

# Create pipeline
python3 gluesync_cli_v2.py create pipeline \
    --name "My Replication Pipeline" \
    --source-type AS400 \
    --target-type MSSQL \
    --source-host 161.82.146.249 \
    --source-user $AS400_USER \
    --source-password $AS400_PASSWORD \
    --target-host 192.168.13.62 \
    --target-user $MSSQL_USER \
    --target-password $MSSQL_PASSWORD \
    --target-database GSTargetDB

# Add entity to pipeline
python3 gluesync_cli_v2.py create entity \
    --pipeline <PIPELINE_ID> \
    --source-library GSLIBTST \
    --source-table CUSTOMERS \
    --target-schema dbo \
    --target-table CUSTOMERS \
    --polling-interval 500 \
    --batch-size 1000

# List entities
python3 gluesync_cli_v2.py list entities --pipeline <PIPELINE_ID>
```

---

## 4. Start Entity (Snapshot + CDC)

```bash
cd ~/_qoder/gluesync-cli

# Start entity with snapshot mode (initial load + ongoing CDC)
python3 gluesync_cli_v2.py start entity <ENTITY_ID> \
    --pipeline <PIPELINE_ID> \
    --mode snapshot

# Check entity status
python3 gluesync_cli_v2.py get entity <ENTITY_ID> --pipeline <PIPELINE_ID>
```

---

## 5. Generate Mockup Data on Source

```bash
cd ~/_qoder/qadmcli

# Generate test data
./qadmcli.sh mockup generate \
    -t CUSTOMERS \
    -l GSLIBTST \
    -n 50 \
    --insert-ratio 60 \
    --update-ratio 30 \
    --delete-ratio 10

# Output shows:
#   Inserted: 30 rows
#   Updated: 15 rows
#   Deleted: 5 rows
```

---

## 6. Check Journal Entries on Source

```bash
cd ~/_qoder/qadmcli

# Get journal summary (operation counts)
./qadmcli.sh journal entries -n CUSTOMERS -l GSLIBTST --format summary

# Get detailed journal entries
./qadmcli.sh journal entries -n CUSTOMERS -l GSLIBTST --limit 50 --format json

# Filter by time range
./qadmcli.sh journal entries -n CUSTOMERS -l GSLIBTST \
    --from-time "2026-04-10 01:00:00" \
    --to-time "2026-04-10 02:00:00" \
    --format summary
```

**Journal Output Format:**
```json
{
  "table": "GSLIBTST.CUSTOMERS",
  "total": 50,
  "inserts": 0,
  "updates": 0,
  "deletes": 0,
  "commits": 0,
  "other": 50,
  "entries": [...]
}
```

---

## 7. Check Change Tracking on Target

```bash
cd ~/_qoder/qadmcli

# Get CT summary (operation counts)
./qadmcli.sh mssql ct changes -t CUSTOMERS -s dbo --format summary

# Get detailed CT changes
./qadmcli.sh mssql ct changes -t CUSTOMERS -s dbo --format json

# Filter by timestamp
./qadmcli.sh mssql ct changes -t CUSTOMERS -s dbo \
    --since "2026-04-10 01:00:00" \
    --format summary

# Filter by version
./qadmcli.sh mssql ct changes -t CUSTOMERS -s dbo \
    --since-version 5 \
    --format summary
```

**CT Output Format:**
```json
{
  "table": "dbo.CUSTOMERS",
  "total": 50,
  "inserts": 30,
  "updates": 15,
  "deletes": 5,
  "current_version": 9,
  "changes": [...]
}
```

---

## 8. Run Comparison Report

```bash
cd ~/_qoder/replica-mon

# Text format (human-readable)
python3 compare.py \
    --source GSLIBTST.CUSTOMERS \
    --target dbo.CUSTOMERS

# JSON format (for automation)
python3 compare.py \
    --source GSLIBTST.CUSTOMERS \
    --target dbo.CUSTOMERS \
    --format json

# Filter by timestamp
python3 compare.py \
    --source GSLIBTST.CUSTOMERS \
    --target dbo.CUSTOMERS \
    --since "2026-04-10 01:00:00"
```

**Report Output:**
```
======================================================================
COMPARISON RESULTS
======================================================================

Operation         AS400 Journal        MSSQL CT   Difference     Status
----------------------------------------------------------------------
INSERT                         30              30           +0         ✅
UPDATE                         15              15           +0         ✅
DELETE                          5               5           +0         ✅
TOTAL                          50              50           +0         ✅
======================================================================

✅ REPLICATION VERIFIED: All operations match!
```

---

## 9. Multi-Table Scenarios

When replicating multiple tables, check each table separately:

```bash
# Define your tables
TABLES=("CUSTOMERS" "ORDERS" "PRODUCTS")
LIBRARY="GSLIBTST"
SCHEMA="dbo"

# Loop through each table
for TABLE in "${TABLES[@]}"; do
    echo "=== Checking $TABLE ==="
    
    # Source journal
    cd ~/_qoder/qadmcli
    ./qadmcli.sh journal entries -n "$TABLE" -l "$LIBRARY" --format summary
    
    # Target CT
    ./qadmcli.sh mssql ct changes -t "$TABLE" -s "$SCHEMA" --format summary
    
    # Comparison report
    cd ~/_qoder/replica-mon
    python3 compare.py --source "$LIBRARY.$TABLE" --target "$SCHEMA.$TABLE"
    
    echo ""
done
```

**Or use the automated demo script:**
```bash
# Single table demo
cd ~/_qoder
./demo_workflow.sh single

# Multi-table demo
./demo_workflow.sh multi
```

---

## 10. Useful Monitoring Commands

```bash
# Check pipeline status
cd ~/_qoder/gluesync-cli
python3 gluesync_cli_v2.py list pipelines

# Check entity status
python3 gluesync_cli_v2.py get entity <ENTITY_ID> --pipeline <PIPELINE_ID>

# List all entities
python3 gluesync_cli_v2.py list entities --pipeline <PIPELINE_ID>

# Check GlueSync logs
ssh ubuntu@192.168.13.53 "tail -f /path/to/gluesync/logs/core-hub.log"
```

---

## 11. Cleanup Commands

```bash
# Stop entity
cd ~/_qoder/gluesync-cli
python3 gluesync_cli_v2.py stop entity <ENTITY_ID> --pipeline <PIPELINE_ID>

# Delete entity
python3 gluesync_cli_v2.py delete entity <ENTITY_ID> --pipeline <PIPELINE_ID>

# Delete pipeline
python3 gluesync_cli_v2.py delete pipeline <PIPELINE_ID>

# Drop AS400 table
cd ~/_qoder/qadmcli
./qadmcli.sh as400 execute -q "DROP TABLE GSLIBTST.CUSTOMERS"

# Drop MSSQL table
./qadmcli.sh mssql execute -q "DROP TABLE dbo.CUSTOMERS"
```

---

## Troubleshooting

### CT Not Enabled
```bash
# Check database CT status
./qadmcli.sh mssql ct status

# Enable if needed
./qadmcli.sh mssql ct enable-db --admin-user $MSSQL_ADMIN_USER --admin-password $MSSQL_ADMIN_PASSWORD
```

### Journal Not Enabled
```bash
# Check journal status
./qadmcli.sh journal info -n CUSTOMERS -l GSLIBTST

# Enable if needed
./qadmcli.sh journal enable -n CUSTOMERS -l GSLIBTST
```

### Replication Lag
```bash
# Wait for replication
sleep 10

# Re-run comparison
cd ~/_qoder/replica-mon
python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS
```

---

## Environment Variables Required

```bash
# AS400
AS400_USER=your_as400_user
AS400_PASSWORD=your_as400_password

# MSSQL
MSSQL_USER=your_mssql_user
MSSQL_PASSWORD=your_mssql_password
MSSQL_ADMIN_USER=your_mssql_admin_user
MSSQL_ADMIN_PASSWORD=your_mssql_admin_password

# GlueSync
GLUESYNC_ADMIN_USERNAME=your_gluesync_admin
GLUESYNC_ADMIN_PASSWORD=your_gluesync_password
```

**⚠️ Security Note:** Use environment variables or a `.env` file. Never hardcode credentials in scripts or documentation.

---

## File Locations

```
~/_qoder/
├── qadmcli/              # Database administration CLI
│   └── qadmcli.sh
├── gluesync-cli/         # GlueSync management CLI
│   └── gluesync_cli_v2.py
├── replica-mon/          # Replication monitoring
│   └── compare.py
├── .env                  # Environment variables
├── demo_workflow.sh      # End-to-end demo script
└── QUICK_REFERENCE.md    # This file
```

