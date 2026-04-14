# Monitor.py Debug Guide

## How to Enable Debug Logging

### Option 1: Verbose Mode
```bash
python3 monitor.py --verbose
# or
python3 monitor.py -v
```

This shows:
- Detailed progress for each entity
- Cache status (hit/miss)
- API query details
- **Exception tracebacks** (if errors occur)

### Option 2: Verbose + No Cache (for debugging)
```bash
python3 monitor.py --verbose --no-cache
```

This disables caching to see raw API responses.

### Option 3: Single Run (not continuous)
```bash
python3 monitor.py --verbose --no-auto-discover
```

Runs once and exits (easier to debug).

---

## Common Issues Seen in Your Terminal Output

### Issue 1: JSON Parse Error
```
⚠️  JSON parse error: Unterminated string starting at: line 592 column 19 (char 37948)
```

**Cause**: Journal entries contain **raw binary data** with special characters that may break JSON parsing.

**Example from qadmcli**:
```json
{
  "after_image": {
    "raw_data": "\u0000\u0000\u0004:\u0000\u000em@@@..."
  }
}
```

**Impact**: 
- Parser fails to extract JSON
- Falls back to returning 0 entries
- Cache shows "Cached 0 new entries"

**Solution**: The parser in `lib/as400_journal.py` needs to handle large JSON with binary data better. The current bracket-counting approach may fail with nested structures containing special characters.

---

### Issue 2: CT Showing 0 Changes
```
ℹ️  Fetching all CT changes (initial load)...
→ ❌ ERROR (Journal: 100, CT: 0)
```

**Likely Cause**: An exception is being caught silently (before we added verbose logging).

**We confirmed**: CT DOES have data when queried directly:
```bash
./qadmcli.sh mssql ct changes -t CUSTOMERS -s dbo --format json
# Returns: Current CT Version: 42, Min Valid Version: 12
# Returns: Array of changes with SYS_CHANGE_VERSION, etc.
```

**With new verbose logging**, you should now see the actual exception:
```
→ ❌ EXCEPTION: <error message here>
→ Traceback:
  File "...", line X, in ...
    <code that failed>
```

---

## How to Debug

### Step 1: Run with verbose and capture output
```bash
cd /home/ubuntu/_qoder/replica-mon
python3 monitor.py --verbose --no-cache 2>&1 | tee /tmp/monitor_debug.log
```

### Step 2: Look for exceptions
```bash
grep -A 10 "EXCEPTION" /tmp/monitor_debug.log
```

### Step 3: Test individual components

**Test journal reader**:
```bash
cd /home/ubuntu/_qoder/qadmcli
./qadmcli.sh journal entries -t CUSTOMERS -l GSLIBTST --format json 2>&1 | head -50
```

**Test CT reader**:
```bash
./qadmcli.sh mssql ct changes -t CUSTOMERS -s dbo --format json 2>&1 | head -30
```

### Step 4: Check if it's a caching issue
```bash
# Clear cache and retry
rm -rf /home/ubuntu/_qoder/replica-mon/cache/
python3 monitor.py --verbose
```

---

## Recent Changes

### Added Verbose Exception Logging
**File**: `monitor.py` (line 429-442)

Now when an exception occurs in verbose mode, you'll see:
```python
if verbose:
    print(f"    → ❌ EXCEPTION: {e}")
    print(f"    → Traceback:")
    for line in traceback.format_exc().split('\n'):
        if line.strip():
            print(f"      {line}")
```

This will help identify exactly where the CT reader is failing.

---

## Next Steps

1. **Run with --verbose** to see the actual CT exception
2. **Check the exception message** - it will tell us what's failing
3. **Fix the root cause** based on the traceback

Most likely issues:
- MSSQL connection error
- Permission issue with CT query
- JSON parsing error in CT response
- Missing table or schema

---

## Quick Test Command

```bash
cd /home/ubuntu/_qoder/replica-mon
timeout 120 python3 monitor.py --verbose --no-cache 2>&1 | grep -E "EXCEPTION|ERROR|Traceback" -A 5
```

This will show you the first exception that occurs within 2 minutes.
