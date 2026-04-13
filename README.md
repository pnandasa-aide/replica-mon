# ReplicaMon - Replication Monitoring & Reconciliation Tool

Compare AS400 journal entries with MSSQL Change Tracking to verify replication integrity and detect discrepancies.

## Overview

ReplicaMon validates that GlueSync replication is working correctly by:
1. Reading AS400 journal entries (source)
2. Reading MSSQL Change Tracking data (target)
3. Comparing operation counts and detecting discrepancies

## Prerequisites

- **qadmcli** - Must be installed and configured for AS400 and MSSQL connections
- **gluesync-cli** - For entity mapping (optional)
- Python 3.8+
- Environment variables configured (see `.env` file)

## Environment Setup

Create a `.env` file in the parent directory (`~/_qoder/.env`):

```bash
# AS400 Source Database
AS400_USER=your_as400_user
AS400_PASSWORD=your_as400_password

# MSSQL Target Database
MSSQL_USER=your_mssql_user
MSSQL_PASSWORD=your_mssql_password

# MSSQL Admin (for CT operations)
MSSQL_ADMIN_USER=your_mssql_admin_user
MSSQL_ADMIN_PASSWORD=your_mssql_admin_password
```

**⚠️ Security Note:** Never commit actual credentials to version control. Use environment variables or a `.env` file (added to `.gitignore`).

## Quick Start

### 1. Automated Monitoring (Recommended)

Monitor all GlueSync entities automatically with intelligent caching:

```bash
cd ~/_qoder/replica-mon

# Single check (auto-discovers entities from GlueSync)
python3 monitor.py

# Continuous monitoring every 5 minutes
python3 monitor.py --continuous

# Continuous monitoring every 1 minute
python3 monitor.py --continuous --interval 60

# JSON output (for dashboards/APIs)
python3 monitor.py --format json

# Check last hour only
python3 monitor.py --since "$(date -d '1 hour ago' '+%Y-%m-%d %H:%M:%S')"
```

**Sample Output:**
```
🔍 Auto-discovering entities from GlueSync...
  📡 Discovered pipeline: 1st pipeline (f590ab8c)
  ✓ Found 3 active entities
  💾 Saved discovered config to: entities.json

📊 Monitoring 3 entities...
========================================================================================================================
  [1/3] Checking GSLIBTST.CUSTOMERS → dbo.CUSTOMERS...
    → ✅ OK (Journal: 34559, CT: 34559)
  [2/3] Checking GSLIBTST.CUSTOMERS2 → dbo.CUSTOMERS2...
    → ✅ OK (Journal: 1234, CT: 1234)
  [3/3] Checking GSLIBTST.ORDERS → dbo.ORDERS...
    → ✅ OK (Journal: 5678, CT: 5678)

========================================================================================================================
REPLICATION MONITORING RESULTS
========================================================================================================================
Source Table              Target Table              Status       Journal       CT   Diff Cache       Attention
------------------------------------------------------------------------------------------------------------------------
GSLIBTST.CUSTOMERS        dbo.CUSTOMERS             ✅ OK          34559    34559      +0 summary    ✓ No
GSLIBTST.CUSTOMERS2       dbo.CUSTOMERS2            ✅ OK           1234     1234      +0 summary    ✓ No
GSLIBTST.ORDERS           dbo.ORDERS                ✅ OK           5678     5678      +0 summary    ✓ No
========================================================================================================================

Summary: 3 OK, 0 Issues, 0 Flagged for Attention
```

### 2. Single Table Comparison (Manual)

For detailed comparison of a specific table:

```bash
cd ~/_qoder/replica-mon

# Text format (human-readable)
python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS

# JSON format (for automation)
python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS --format json

# Filter by timestamp
python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS --since "2026-04-10 01:00:00"
```

### 2. Sample Output

**Text Format:**
```
======================================================================
REPLICATION COMPARISON REPORT
======================================================================
Generated: 2026-04-10 02:15:30
Source (AS400): GSLIBTST.CUSTOMERS
Target (MSSQL): dbo.CUSTOMERS

[1/3] Querying AS400 journal...
  ✓ Retrieved 50 journal entries
[2/3] Querying MSSQL Change Tracking...
  ✓ Retrieved 50 CT changes
[3/3] Comparing...

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

**JSON Format:**
```json
{
  "timestamp": "2026-04-10T02:15:30",
  "source_table": "GSLIBTST.CUSTOMERS",
  "target_table": "dbo.CUSTOMERS",
  "since": null,
  "journal_summary": {
    "table": "GSLIBTST.CUSTOMERS",
    "total": 50,
    "inserts": 30,
    "updates": 15,
    "deletes": 5
  },
  "ct_summary": {
    "table": "dbo.CUSTOMERS",
    "total": 50,
    "inserts": 30,
    "updates": 15,
    "deletes": 5,
    "current_version": 9
  },
  "comparison": {
    "difference": 0,
    "discrepancies": [],
    "match": true
  }
}
```

## Advanced Usage

### Cache Management

ReplicaMon uses intelligent tiered caching for fast performance:

```bash
# View cache status for all tables
python3 compare.py --cache-info

# View cache for specific table
python3 compare.py --cache-info --source GSLIBTST.CUSTOMERS

# Clear all caches
python3 compare.py --clear-cache

# Clear cache for specific table
python3 compare.py --clear-cache --source GSLIBTST.CUSTOMERS

# List tables requiring attention (discrepancies detected)
python3 compare.py --list-attention

# Reset attention flag for a table
python3 compare.py --reset-attention --source GSLIBTST.CUSTOMERS

# Disable caching (always query AS400)
python3 monitor.py --no-cache
```

### How Caching Works

**Tier 1: Summary Cache (Default)**
- Stores operation counts (~1 KB per table)
- Used for hourly monitoring
- Fast subsequent checks (1 sec vs 60 sec)

**Tier 2: Full Entry Cache (Auto-upgrade)**
- Automatically triggered when discrepancy detected
- Stores complete journal entries
- Used for detailed investigation
- Can be reset after issue resolved

**Cache Update Strategy:**
1. First run: Query AS400 (slow, builds cache)
2. Subsequent runs: Use cache + incremental updates (fast!)
3. Discrepancy detected: Auto-flag table for full caching
4. Manual review: Investigate flagged tables
5. Reset flag: Return to lightweight summary cache

### Monitoring Workflow

```bash
# Start continuous monitoring (runs in background)
python3 monitor.py --continuous --interval 300 &

# Check status anytime
python3 monitor.py --format json | python3 -m json.tool

# View flagged tables
python3 compare.py --list-attention

# Stop monitoring
kill %1  # or find PID and kill
```

### Using qadmcli Directly

You can also use qadmcli commands directly for more control:

**AS400 Journal Summary:**
```bash
cd ~/_qoder/qadmcli
source ../.env
export AS400_USER AS400_PASSWORD

# Get summary
./qadmcli.sh journal entries -n CUSTOMERS -l GSLIBTST --format summary

# Filter by time range
./qadmcli.sh journal entries -n CUSTOMERS -l GSLIBTST \
  --from-time "2026-04-10 01:00:00" \
  --to-time "2026-04-10 02:00:00" \
  --format summary
```

**MSSQL Change Tracking Summary:**
```bash
cd ~/_qoder/qadmcli
source ../.env
export MSSQL_USER MSSQL_PASSWORD

# Get summary
./qadmcli.sh mssql ct changes -t CUSTOMERS -s dbo --format summary

# Filter by timestamp
./qadmcli.sh mssql ct changes -t CUSTOMERS -s dbo \
  --since "2026-04-10 01:00:00" \
  --format summary

# Filter by version
./qadmcli.sh mssql ct changes -t CUSTOMERS -s dbo \
  --since-version 5 \
  --format summary
```

### Using Library Classes in Python

```python
from lib.as400_journal import AS400JournalReader
from lib.mssql_ct import MSSQLCTReader

# AS400 Journal
journal = AS400JournalReader()
summary = journal.get_summary("GSLIBTST.CUSTOMERS", since="2026-04-10 01:00:00")
print(f"Journal inserts: {summary['inserts']}")

# MSSQL CT
ct = MSSQLCTReader()
ct_summary = ct.get_summary("dbo.CUSTOMERS", since="2026-04-10 01:00:00")
print(f"CT inserts: {ct_summary['inserts']}")
```

## CLI Reference

### monitor.py - Automated Entity Monitoring

```bash
python3 monitor.py [OPTIONS]

Options:
  --config FILE          Use specific entity config file
  --since TIMESTAMP      Filter changes since timestamp
  --format table|json    Output format (default: table)
  --continuous           Run in continuous monitoring mode
  --interval SECONDS     Check interval in seconds (default: 300)
  --no-cache             Disable journal caching
  --no-cache-status      Hide cache status in table
  --no-auto-discover     Disable GlueSync auto-discovery

Examples:
  # Single check with auto-discovery
  python3 monitor.py

  # Continuous monitoring every 5 minutes
  python3 monitor.py --continuous

  # JSON output for dashboard
  python3 monitor.py --format json

  # Check last hour only
  python3 monitor.py --since "$(date -d '1 hour ago' '+%Y-%m-%d %H:%M:%S')"
```

### compare.py - Single Table Comparison

```bash
python3 compare.py --source LIBRARY.TABLE --target SCHEMA.TABLE [OPTIONS]

Options:
  --source TABLE         AS400 source table (LIBRARY.TABLE)
  --target TABLE         MSSQL target table (SCHEMA.TABLE)
  --since TIMESTAMP      Filter changes since timestamp
  --format table|json    Output format (default: table)
  --timezone-only        Show timezone info and exit
  --no-timezone          Hide timezone information
  --no-cache             Disable journal caching
  --cache-info           Show cache information and exit
  --clear-cache          Clear journal cache and exit
  --reset-attention      Reset attention flag for table
  --list-attention       List all tables requiring attention

Examples:
  # Compare single table
  python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS

  # Compare with time filter
  python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS \
    --since "2026-04-13 00:00:00"

  # JSON output for automation
  python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS \
    --format json
```

## Understanding the Output

### Journal Entry Codes (AS400)

| Code | Operation | Description |
|------|-----------|-------------|
| `PT` | INSERT | Put/Insert operation |
| `UP` | UPDATE | Update operation |
| `DL` | DELETE | Delete operation |
| `CG` | COMMIT | Commit/Group |
| `JF` | COMMIT | Journal File |

### CT Operation Codes (MSSQL)

| Code | Operation | Description |
|------|-----------|-------------|
| `I` | INSERT | Row inserted |
| `U` | UPDATE | Row updated |
| `D` | DELETE | Row deleted |

### Discrepancy Examples

If replication is not working correctly, you might see:

```
Operation         AS400 Journal        MSSQL CT   Difference     Status
----------------------------------------------------------------------
INSERT                         30              28           +2         ❌
UPDATE                         15              15           +0         ✅
DELETE                          5               5           +0         ✅
TOTAL                          50              48           +2         ❌
======================================================================

⚠️  DISCREPANCY DETECTED!

Discrepancies:
  - Total count mismatch: source=50, target=48
  - Inserts count mismatch: source=30, target=28
```

This indicates 2 INSERT operations were not replicated to MSSQL.

## Automation Examples

### Cron Job for Hourly Checks

```bash
# Add to crontab
0 * * * * cd /home/ubuntu/_qoder/replica-mon && \
  python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS \
  --format json >> /var/log/replica-mon/hourly_check.json 2>&1
```

### Alert on Discrepancy

```bash
#!/bin/bash
# check_replication.sh

RESULT=$(python3 compare.py --source GSLIBTST.CUSTOMERS --target dbo.CUSTOMERS --format json)
MATCH=$(echo "$RESULT" | python3 -c "import sys, json; print(json.load(sys.stdin)['comparison']['match'])")

if [ "$MATCH" = "False" ]; then
    echo "ALERT: Replication discrepancy detected!" | mail -s "ReplicaMon Alert" admin@example.com
    echo "$RESULT" >> /var/log/replica-mon/alerts.log
fi
```

## Troubleshooting

### qadmcli not found
Ensure qadmcli is in the correct location and executable:
```bash
ls -la ../qadmcli/qadmcli.sh
chmod +x ../qadmcli/qadmcli.sh
```

### Environment variables not set
Source the .env file:
```bash
cd ~/_qoder
source .env
export AS400_USER AS400_PASSWORD MSSQL_USER MSSQL_PASSWORD
```

### Connection errors
Test qadmcli connections:
```bash
# Test AS400
./qadmcli.sh journal info -n CUSTOMERS -l GSLIBTST

# Test MSSQL
./qadmcli.sh mssql ct status -t CUSTOMERS -s dbo
```

## Architecture

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
│   AS400         │         │   ReplicaMon     │         │   MSSQL         │
│   Journal       │─────┬──▶│                  │◀────┬───▶│   Change        │
│  (Source)       │     │   │  • monitor.py    │     │    │   Tracking      │
└─────────────────┘     │   │  • compare.py    │     │    └─────────────────┘
                        │   │  • Auto-discovery│     │
                        │   │  • Smart caching │     │
                        │   └──────────────────┘     │
                        │                            │
                        │   ┌──────────────────┐     │
                        └──▶│  GlueSync        │◀────┘
                            │  Replication     │
                            └──────────────────┘
```

### Key Features

- **Auto-Discovery**: Automatically detects entities from GlueSync pipeline
- **Intelligent Caching**: Summary + full entry tiered caching
- **Auto-Flagging**: Automatically flags tables with discrepancies
- **Continuous Monitoring**: Scheduled checks with configurable intervals
- **Timezone-Aware**: Handles AS400 (UTC+0) vs MSSQL (UTC+7) differences
- **JSON Output**: Ready for dashboards, APIs, and automation

## Git Workflow

This project follows standard Git workflow practices. See [`GIT_WORKFLOW.md`](../GIT_WORKFLOW.md) for details.

### Recent Changes

- **v0.3.0** - Automated monitoring with auto-discovery
  - Added monitor.py for continuous entity monitoring
  - Auto-discovery from GlueSync CLI (no manual config needed)
  - Intelligent tiered caching (summary + full entry)
  - Auto-flagging tables with discrepancies
  - Timezone-aware comparisons (AS400 UTC+0, MSSQL UTC+7)
  - JSON output for dashboards and APIs

- **v0.2.0** - Enhanced caching and timezone handling
  - Journal caching for 60-120x performance improvement
  - Automatic timezone detection and normalization
  - Cache management CLI commands
  - Attention flag system for problematic tables

- **v0.1.0** - Initial release
  - Basic comparison report feature
  - AS400 journal and MSSQL CT integration
  - Time-based filtering

## License

Internal use only.
