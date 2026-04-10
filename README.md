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

### 1. Generate Comparison Report

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
┌─────────────┐         ┌──────────────┐         ┌─────────────┐
│   AS400     │         │  ReplicaMon  │         │    MSSQL    │
│   Journal   │─────┬──▶│  Comparison  │◀────┬───▶│  Change     │
│  (Source)   │     │   │   Report     │     │    │ Tracking    │
└─────────────┘     │   └──────────────┘     │    └─────────────┘
                    │                        │
                    │   ┌──────────────┐     │
                    └──▶│  GlueSync    │◀────┘
                        │  Replication │
                        └──────────────┘
```

## Git Workflow

This project follows standard Git workflow practices. See [`GIT_WORKFLOW.md`](../GIT_WORKFLOW.md) for details.

### Recent Changes

- **v0.1.0** - Initial release with comparison report feature
- Enhanced qadmcli with `--format summary` for both journal and CT
- Added time-based filtering (`--from-time`, `--to-time`, `--since`)

## License

Internal use only.
