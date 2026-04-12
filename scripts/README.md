# Test Monitor - Automated Replication Testing

Automated test suite that generates mockup data and verifies CDC replication across multiple tables.

## Overview

The test monitor automates the complete replication testing workflow:

1. **Generate mockup data** for specified tables
2. **Wait for CDC replication** to complete
3. **Verify replication** using journal/CT comparison
4. **Check row counts** between source and target
5. **Generate reports** with detailed results

## Quick Start

### Test All Tables (Default)

```bash
cd /home/ubuntu/_qoder/replica-mon/scripts

# Run with defaults (CUSTOMERS, CUSTOMERS2, ORDERS)
./test_monitor.sh

# Or use Python directly
python3 test_monitor.py
```

### Test Specific Tables

```bash
# Test only CUSTOMERS and CUSTOMERS2
./test_monitor.sh --tables CUSTOMERS,CUSTOMERS2

# Test only ORDERS
./test_monitor.sh --tables ORDERS
```

### Customize Test Parameters

```bash
# Generate 20 rows per table, wait 3 minutes for replication
./test_monitor.sh --rows 20 --wait 180

# Quick test (less wait time)
./test_monitor.sh --wait 60
```

### Save Reports

```bash
# Save JSON report
./test_monitor.sh --format json --output report_20260412.json

# Test specific tables and save report
./test_monitor.sh --tables CUSTOMERS --rows 15 --output customers_test.json
```

## Command-Line Options

| Option | Description | Default |
|--------|-------------|---------|
| `--tables` | Comma-separated table names | `CUSTOMERS,CUSTOMERS2,ORDERS` |
| `--rows` | Mockup rows per table | `10` |
| `--wait` | Seconds to wait for CDC | `120` |
| `--format` | Output format (text/json) | `text` |
| `--output` | Save report to file | None |
| `--library` | AS400 library name | `GSLIBTST` |
| `--schema` | MSSQL target schema | `dbo` |

## Example Output

```
######################################################################
# REPLICATION TEST MONITOR
######################################################################
  Started: 2026-04-12 21:15:00
  Tables: CUSTOMERS, CUSTOMERS2, ORDERS
  Rows per table: 10
  CDC wait time: 120s
  Output format: text
######################################################################

######################################################################
# Testing Table: CUSTOMERS
######################################################################

──────────────────────────────────────────────────────────────────────
  Step 1: Generating mockup data for CUSTOMERS
──────────────────────────────────────────────────────────────────────
Command: ../qadmcli/qadmcli.sh mockup generate -t CUSTOMERS -l GSLIBTST -n 10
✅ Mockup data generated successfully

──────────────────────────────────────────────────────────────────────
  Step 2: Waiting 120s for CDC replication...
──────────────────────────────────────────────────────────────────────
  (CDC typically replicates within 30-60 seconds)
  Table: CUSTOMERS
  ⏱️  10 seconds remaining...

──────────────────────────────────────────────────────────────────────
  Step 3: Checking replication for CUSTOMERS
──────────────────────────────────────────────────────────────────────
  Source: GSLIBTST.CUSTOMERS
  Target: dbo.customers
======================================================================
REPLICATION COMPARISON REPORT
======================================================================
...
✅ REPLICATION VERIFIED: All operations match!

######################################################################
# Testing Table: CUSTOMERS2
######################################################################
...

======================================================================
  TEST SUMMARY
======================================================================
  Timestamp: 2026-04-12T21:17:00
  Tables Tested: 3
  ✅ Successful: 3
  ⚠️  Warnings: 0
  ❌ Failed: 0
======================================================================

Table                Mockup       Replication     Row Counts   Status    
──────────────────────────────────────────────────────────────────────────
CUSTOMERS            ✅           ✅              ✅           ✅ PASS   
CUSTOMERS2           ✅           ✅              ✅           ✅ PASS   
ORDERS               ✅           ✅              ✅           ✅ PASS   
──────────────────────────────────────────────────────────────────────────

🎉 ALL TESTS PASSED!
```

## How It Works

### Test Flow

For each table:

1. **Generate Mockup Data**
   - Calls `qadmcli mockup generate`
   - Creates realistic INSERT, UPDATE, DELETE operations
   - Respects database constraints and data types

2. **Wait for CDC**
   - Default: 120 seconds
   - CDC typically completes in 30-60 seconds
   - Shows countdown timer

3. **Verify Replication**
   - Runs `compare.py` to check journal vs CT
   - Compares operation counts (INSERT/UPDATE/DELETE)
   - Detects discrepancies

4. **Count Rows**
   - Queries both AS400 and MSSQL
   - Verifies row counts match
   - Reports any differences

5. **Report Results**
   - Individual table status
   - Summary with pass/fail counts
   - Optional JSON report file

## Use Cases

### 1. Pre-Production Validation

```bash
# Test all tables before going live
./test_monitor.sh --rows 50 --wait 180 --output pre_prod_validation.json
```

### 2. Post-Maintenance Check

```bash
# Quick check after system maintenance
./test_monitor.sh --tables CUSTOMERS --wait 60
```

### 3. Performance Testing

```bash
# Generate large dataset and verify
./test_monitor.sh --rows 100 --wait 300 --output perf_test.json
```

### 4. Scheduled Monitoring

```bash
# Add to crontab for hourly checks
0 * * * * cd /home/ubuntu/_qoder/replica-mon/scripts && ./test_monitor.sh --wait 180 --output /var/log/replica-mon/$(date +\%Y\%m\%d_\%H).json
```

## Troubleshooting

### Mockup Generation Fails

**Symptom**: `❌ Mockup generation failed`

**Solutions**:
1. Verify table exists in AS400:
   ```bash
   ../qadmcli/qadmcli.sh sql execute -q "SELECT * FROM GSLIBTST.CUSTOMERS FETCH FIRST 1 ROWS ONLY"
   ```

2. Check table constraints:
   ```bash
   ../qadmcli/qadmcli.sh sql execute -q "SELECT COLUMN_NAME, DATA_TYPE, LENGTH, SCALE FROM QSYS2.SYSCOLUMNS WHERE TABLE_SCHEMA='GSLIBTST' AND TABLE_NAME='CUSTOMERS'"
   ```

### Replication Check Times Out

**Symptom**: Test waits full duration but CDC hasn't replicated

**Solutions**:
1. Increase wait time: `--wait 180`
2. Check GlueSync status:
   ```bash
   cd ../gluesync-cli
   ./gluesync_cli_v2.py entity list
   ```
3. Verify CDC is enabled on entity

### Row Count Mismatch

**Symptom**: `⚠️  Row count mismatch: X rows`

**Possible Causes**:
- CDC still replicating (increase `--wait`)
- Failed operations (check comparison report)
- Manual deletions outside CDC

**Investigation**:
```bash
# Detailed comparison
python3 ../compare.py --source GSLIBTST.CUSTOMERS --target dbo.customers
```

## Integration with CI/CD

### GitHub Actions Example

```yaml
name: Replication Test
on: [push, schedule]

jobs:
  test-replication:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      
      - name: Run Replication Test
        run: |
          cd replica-mon/scripts
          ./test_monitor.sh --format json --output test_results.json
      
      - name: Upload Results
        uses: actions/upload-artifact@v2
        with:
          name: replication-report
          path: replica-mon/scripts/test_results.json
```

## Files

| File | Description |
|------|-------------|
| `test_monitor.py` | Main Python script |
| `test_monitor.sh` | Bash wrapper script |
| `README.md` | This documentation |

## Requirements

- Python 3.6+
- qadmcli installed and configured
- GlueSync CLI access
- AS400 and MSSQL connectivity
- Change Tracking enabled on target tables

## Next Steps

1. ✅ Test all three tables (CUSTOMERS, CUSTOMERS2, ORDERS)
2. ✅ Customize row counts and wait times
3. ✅ Generate JSON reports for auditing
4. 🔲 Add email notifications on failure
5. 🔲 Integrate with monitoring dashboards
6. 🔲 Add historical trend analysis

---

*For more information, see [replica-mon README](../README.md)*
