# Test Traffic Generation Guide

## Quick Start

### Generate All Operations (Default)
```bash
cd /home/ubuntu/_qoder/replica-mon

# Generate 10 inserts, 5 updates, 3 deletes per table
python3 generate_test_traffic.py

# Generate 20 inserts, 10 updates, 6 deletes per table
python3 generate_test_traffic.py all 20
```

### Generate Specific Operations
```bash
# Inserts only (10 per table)
python3 generate_test_traffic.py insert

# Inserts only (50 per table)
python3 generate_test_traffic.py insert 50

# Updates only (5 per table)
python3 generate_test_traffic.py update

# Deletes only (3 per table)
python3 generate_test_traffic.py delete
```

### Check Replication Status
```bash
# Run monitor to see replication status
python3 monitor.py

# Continuous monitoring (every 5 minutes)
python3 monitor.py --continuous --interval 300
```

## What It Does

1. **INSERT Operations**: Adds new records with unique primary keys
   - ORDERS: ORDER_ID, ORDER_DATE, CUSTOMER_ID, AMOUNT, STATUS
   - CUSTOMERS: CUST_ID, NAME, EMAIL, CREATED_DATE
   - CUSTOMERS2: CUST_ID, NAME, EMAIL, CREATED_DATE

2. **UPDATE Operations**: Modifies existing records
   - ORDERS: Updates AMOUNT and STATUS
   - CUSTOMERS/CUSTOMERS2: Updates NAME and EMAIL

3. **DELETE Operations**: Removes oldest records

4. **Automatic Monitor**: After generating traffic, waits 30 seconds then runs monitor.py

## Workflow Example

```bash
# Step 1: Check current status
python3 monitor.py

# Step 2: Generate test traffic (50 inserts per table)
python3 generate_test_traffic.py insert 50

# Step 3: Wait for replication (script does this automatically)
# Script waits 30 seconds, then runs monitor

# Step 4: Check if counts match
python3 monitor.py

# Step 5: If mismatch, investigate
python3 diagnose.py
```

## Understanding Monitor Output

```
Source Table              Target Table              Status           Journal       CT   Diff
--------------------------------------------------------------------------------------------
GSLIBTST.CUSTOMERS        dbo.CUSTOMERS             ✅ OK               50       50     +0
GSLIBTST.ORDERS           dbo.ORDERS                ❌ MISMATCH        100       95     +5
GSLIBTST.CUSTOMERS2       dbo.CUSTOMERS2            ⚠️  PREREQ FAILED     0        0     +0
```

- **✅ OK**: Source and target counts match
- **❌ MISMATCH**: Replication lag or errors (Journal ≠ CT)
- **⚠️ PREREQ FAILED**: Journaling or Change Tracking not enabled

## Troubleshooting

### No Changes Detected
```bash
# Verify journal entries exist
../qadmcli/qadmcli.sh journal entries -t CUSTOMERS -l GSLIBTST --format summary

# Verify CT changes exist
../qadmcli/qadmcli.sh mssql ct changes -t CUSTOMERS -s dbo --format summary
```

### Replication Stuck
```bash
# Check GlueSync status
ps aux | grep gluesync

# Check recent journal entries
../qadmcli/qadmcli.sh journal entries -t CUSTOMERS -l GSLIBTST --last 10
```

### Clear Cache and Recheck
```bash
# Clear all caches
rm -rf cache/

# Run monitor with fresh queries
python3 monitor.py --no-cache
```

## Tips

1. **Start Small**: Test with 10 inserts first to verify replication works
2. **Monitor Logs**: Check GlueSync logs for errors during replication
3. **Timing**: Replication typically takes 5-30 seconds depending on configuration
4. **Large Tests**: For 100+ records, increase wait time or use continuous monitoring

## Safety Notes

- ✅ All operations use test data with unique timestamps
- ✅ Deletes only remove oldest records (safe for testing)
- ✅ Primary keys use timestamp-based values to avoid collisions
- ⚠️ Don't run on production tables without proper backup
- ⚠️ Monitor disk space when generating large amounts of test data
