# Entity Status Test Results - CRITICAL FINDINGS

**Date:** 2026-04-07  
**Test:** Test 1 (Check entity status from GlueSync API)

---

## 🎯 Key Findings

### Finding 1: Status Field is "N/A"

**ALL entities return:** `Status: 'N/A'`

```
ENTITY 1: GSLIBTST.CUSTOMERZ
  Status: 'N/A'

ENTITY 2: GSLIBTST.CUSTOMERS
  Status: 'N/A'
```

**Conclusion:** GlueSync's `list_entities()` API does **NOT** provide real-time entity status (running/stopped).

### Finding 2: EntityType Shows "Source" and "Target"

**NOT** "CDC" or "SNAPSHOT" as we expected!

```
Agent 1 (Source):
  EntityType: {
    "type": "Source",
    "maxFetchItemsCountPerIteration": 10000,
    ...
  }

Agent 2 (Target):
  EntityType: {
    "type": "Target",
    "allowedOperations": ["INSERT", "UPDATE", "DELETE", "TRUNCATE"],
    ...
  }
```

**Conclusion:** No indication of CDC vs Snapshot mode in entityType.

### Finding 3: No Snapshot-Related Fields

Searched for these fields in entity response:
- ❌ `lastStartedAt` - NOT FOUND
- ❌ `lastSnapshotAt` - NOT FOUND
- ❌ `snapshotStatus` - NOT FOUND
- ❌ `startHistory` - NOT FOUND
- ❌ `completedAt` - NOT FOUND

**Available fields:** `['entityId', 'entityName', 'agentEntities', 'groupId', 'orderIndex']`

---

## 📊 What GlueSync API Provides

### Entity Response Structure:

```json
{
  "entityId": "a62bd1b6",
  "entityName": "GSLIBTST.CUSTOMERZ",
  "agentEntities": [
    {
      "type": "Source",
      "entityId": "...",
      "entityName": "...",
      "agentEntityId": "...",
      "entityType": {
        "type": "Source",
        "maxFetchItemsCountPerIteration": 10000,
        "maxTransactionMessageKbSize": 1024,
        "pollingIntervalMilliseconds": 500,
        "unchangedDataFilterType": "ENTIRE_ROW"
      },
      "agentId": "...",
      "orderIndex": 0,
      "customProperties": {},
      "tablesProperties": {},
      "table": {
        "schema": "GSLIBTST",
        "name": "CUSTOMERZ"
      },
      "columns": [...],
      "keys": [...]
    },
    {
      "type": "Target",
      "entityType": {
        "type": "Target",
        "allowedOperations": ["INSERT", "UPDATE", "DELETE", "TRUNCATE"],
        "snapshotWritingConcurrency": 2,
        "mappingFunctionInfo": {
          "name": "UDF_xxx",
          "type": "Java"
        },
        ...
      },
      ...
    }
  ],
  "groupId": "...",
  "orderIndex": 0
}
```

**No runtime status information!**

---

## 🤔 Implications for Task 3 (Entity Status)

### Original Plan:
- Extract status from GlueSync entity response
- Show badges: CDC Active / Snapshotting / Snapshot Done / Not Started

### Reality:
- ❌ GlueSync doesn't provide runtime status via API
- ❌ Cannot distinguish "Never Started" vs "Snapshot Completed"
- ❌ Cannot detect if entity is actively running CDC

### Alternative Approaches:

#### Option 1: Infer from Target Table Row Count
```python
def infer_entity_status(entity):
    # Get target table
    target = entity['agentEntities'][1]  # Target agent
    schema = target['table']['schema']
    table = target['table']['name']
    
    # Count rows
    count = _count_mssql(schema, table)[0]
    
    if count == 0:
        return "Not Started"  # or "Empty target"
    else:
        return "Has Data"  # Snapshot ran at some point
```

**Pros:** Simple  
**Cons:** 
- Cannot distinguish "actively replicating" vs "snapshot done, CDC not started"
- Slow (requires DB query for each entity)
- What if target legitimately has 0 rows?

#### Option 2: Check GlueSync Metrics/Logs
- WebSocket metrics show operation counts
- If receiving metrics → entity is active
- If no metrics → entity might be stopped

**Pros:** Real-time  
**Cons:** 
- Requires WebSocket connection
- Might not distinguish "not started" from "stopped after snapshot"

#### Option 3: Check Agent Process/Logs
- Query agent containers to see if they're running
- Check agent logs for activity

**Pros:** Accurate  
**Cons:** 
- Complex (need access to agent containers)
- Might not work with all deployment types

#### Option 4: Add Manual Status Tracking
- Store status in our own database when user starts/stops entity
- Track: `last_started_at`, `last_stopped_at`, `snapshot_completed_at`

**Pros:** Full control  
**Cons:** 
- Requires us to intercept start/stop API calls
- Might get out of sync if user uses GlueSync UI directly

---

## 📝 Recommended Approach

### Phase 1: Simple (Current Sprint)

**Show generic status based on data presence:**

```python
def get_entity_status_simple(entity):
    """
    Simple status based on what we can detect
    """
    target = entity['agentEntities'][1]
    schema = target['table']['schema']
    table = target['table']['name']
    
    try:
        count = _count_mssql(schema, table)[0]
        if count > 0:
            return {
                'display': 'Active',
                'color': 'green',
                'icon': '✅',
                'tooltip': f'Target has {count:,} rows'
            }
        else:
            return {
                'display': 'No Data',
                'color': 'gray',
                'icon': '⬜',
                'tooltip': 'Target table is empty'
            }
    except:
        return {
            'display': 'Unknown',
            'color': 'yellow',
            'icon': '❓',
            'tooltip': 'Cannot check target table'
        }
```

### Phase 2: Enhanced (Future)

**Integrate with WebSocket metrics:**
- If receiving real-time metrics → "CDC Active"
- If no metrics but target has data → "Snapshot Done"
- If no metrics and target empty → "Not Started"

---

## 🚀 Next Steps

1. **Update Task 3 implementation** to use target row count instead of GlueSync status
2. **Test with actual entities** to verify this approach works
3. **Consider Phase 2 enhancement** with WebSocket metrics integration
4. **Document limitation** - GlueSync API doesn't provide runtime status

---

## ⚠️ Important Note

**Your clarification about "stopped" status was correct** - it IS ambiguous. However, GlueSync doesn't provide the information we need to distinguish the states. We must **infer status from indirect indicators** (target row count, WebSocket metrics).

This is a **GlueSync API limitation**, not an implementation issue.

---

**Bottom Line:** We cannot get "CDC Active" vs "Snapshotting" vs "Snapshot Done" from GlueSync API. We must detect it ourselves by checking target table and/or monitoring WebSocket metrics.
