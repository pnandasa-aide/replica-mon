# Test Monitor - Quick Reference

## What It Does

Automated end-to-end replication testing:
1. Generates mockup data for your tables
2. Waits for CDC replication
3. Verifies replication success
4. Generates detailed reports

## Quick Commands

### Test All Tables (Default)
```bash
cd /home/ubuntu/_qoder/replica-mon/scripts
./test_monitor.sh
```

### Test Specific Tables
```bash
./test_monitor.sh --tables CUSTOMERS,CUSTOMERS2
```

### Customize Test
```bash
./test_monitor.sh --rows 20 --wait 180
```

### Save JSON Report
```bash
./test_monitor.sh --format json --output report.json
```

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--tables` | CUSTOMERS,CUSTOMERS2,ORDERS | Tables to test |
| `--rows` | 10 | Mockup rows per table |
| `--wait` | 120 | Seconds to wait for CDC |
| `--format` | text | Output format (text/json) |
| `--output` | None | Save report to file |

## Example Output

```
######################################################################
# Testing Table: CUSTOMERS
######################################################################

Step 1: Generating mockup data for CUSTOMERS
✅ Mockup data generated successfully

Step 2: Waiting 120s for CDC replication...
⏱️  10 seconds remaining...

Step 3: Checking replication for CUSTOMERS
✅ REPLICATION VERIFIED: All operations match!

======================================================================
  TEST SUMMARY
======================================================================
  Tables Tested: 3
  ✅ Successful: 3
  ⚠️  Warnings: 0
  ❌ Failed: 0
======================================================================

Table                Status    
────────────────────────────────
CUSTOMERS            ✅ PASS   
CUSTOMERS2           ✅ PASS   
ORDERS               ✅ PASS   

🎉 ALL TESTS PASSED!
```

## Files Created

- `scripts/test_monitor.py` - Main Python script (563 lines)
- `scripts/test_monitor.sh` - Bash wrapper (35 lines)
- `scripts/README.md` - Full documentation (297 lines)

## Git Commit

```
Commit: 2467545
Message: feat: Add automated test monitor for replication verification
Files: 3 new files, 892 insertions
```

---

*See scripts/README.md for complete documentation*
