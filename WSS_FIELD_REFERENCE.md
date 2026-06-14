# WebSocket Metrics Stream - Field Reference Guide

## Endpoint
```
ws://192.168.13.53:8000/ws/metrics?pipeline_id={id}&entities={name1},{name2}
```

## Overview

The WebSocket endpoint streams real-time replication metrics from GlueSync Core Hub. The backend **enriches** raw Protobuf messages with metadata from GlueSync REST API to provide human-readable labels, field descriptions, and formatted timestamps.

### Metadata Enrichment Process

1. **On WebSocket connection**, backend queries GlueSync REST API:
   - `GET /pipelines` → Gets pipeline list with names
   - `GET /pipelines/{id}/entities` → Gets entity list with names and table mappings

2. **Builds metadata cache** mapping:
   - Entity name → Entity metadata (ID, source table, target table)
   - Entity ID → Entity metadata (for reverse lookup)

3. **For each incoming message**, backend:
   - Extracts raw entity identifier (could be name OR ID)
   - Looks up in metadata cache (tries name first, then ID)
   - Adds `_enriched` field with human-readable data
   - Forwards enriched JSON to browser

**Key Feature**: Even if GlueSync sends entity as an ID (e.g., `"4bacd683"`), the enrichment will resolve it to the proper name from metadata (e.g., `"GSLIBTST.THAI_TEST"`).

---

## Message Types

The WebSocket stream sends two types of messages from GlueSync:

### 1. **MetricsMessage** - Real-time replication operation counts

**What it represents**: Cumulative counts of INSERT, UPDATE, DELETE operations processed by GlueSync for each entity since the last metrics update.

**Raw Protobuf Structure (from GlueSync):**
```json
{
  "Field_1_string": "MetricsMessage",
  "Field_2_message": {
    "Field_1_message": {
      "Field_1_message": {
        "Field_2_message": {
          "Field_1_string": "GSLIBTST.CUSTOMERS",      // Entity name OR ID (varies)
          "Field_2_string": "2026-05-21T08:01:35Z",    // Timestamp (ISO format)
          "Field_4_varint": 150,                        // INSERT count
          "Field_5_varint": 320,                        // UPDATE count
          "Field_6_varint": 12,                         // DELETE count
          "Field_7_varint": 482                         // Total operations
        }
      }
    }
  }
}
```

**⚠️ Important**: `Field_1_string` may contain either:
- Entity **name**: `"GSLIBTST.CUSTOMERS"` ✅
- Entity **ID**: `"2f7b032d"` ⚠️

The enrichment logic handles both cases by looking up in metadata cache.

**Enriched Output (what browser receives):**
```json
{
  "Field_1_string": "MetricsMessage",
  "Field_2_message": {
    "Field_1_message": {
      "Field_1_message": {
        "Field_2_message": {
          "Field_1_string": "GSLIBTST.CUSTOMERS",      // Entity name
          "Field_2_string": "2026-05-21T08:01:35Z",    // Timestamp (ISO format)
          "Field_4_varint": 150,                        // INSERT count
          "Field_5_varint": 320,                        // UPDATE count
          "Field_6_varint": 12,                         // DELETE count
          "Field_7_varint": 482                         // Total operations
        }
      }
    }
  }
}
```

**Enriched Output (with `_enriched` field):**
```json
{
  "Field_1_string": "MetricsMessage",
  "Field_2_message": {
    "Field_1_message": {
      "Field_1_message": {
        "Field_2_message": {
          "Field_1_string": "GSLIBTST.CUSTOMERS",
          "Field_2_string": "2026-05-21T08:01:35Z",
          "Field_4_varint": 150,
          "Field_5_varint": 320,
          "Field_6_varint": 12,
          "Field_7_varint": 482,
          "_enriched": {
            "entity_display": "2f7b032d \"GSLIBTST.CUSTOMERS\" entity",
            "source_table": "GSLIBTST.CUSTOMERS",
            "target_table": "dbo.CUSTOMERS",
            "timestamp_human": "2026-05-21 08:01:35 UTC",
            "fields": {
              "Field_4_varint": {
                "name": "inserts",
                "value": 150,
                "description": "Number of INSERT operations"
              },
              "Field_5_varint": {
                "name": "updates",
                "value": 320,
                "description": "Number of UPDATE operations"
              },
              "Field_6_varint": {
                "name": "deletes",
                "value": 12,
                "description": "Number of DELETE operations"
              },
              "Field_7_varint": {
                "name": "total_ops",
                "value": 482,
                "description": "Total operations (inserts + updates + deletes)"
              }
            }
          }
        }
      }
    }
  }
}
```

**Field Meanings:**

| Field | Type | Meaning | Example |
|-------|------|---------|---------|
| `Field_1_string` | String | **Entity Name** - Source table being replicated | `"GSLIBTST.CUSTOMERS"` |
| `Field_2_string` | String | **Timestamp** - When metrics were captured (ISO 8601) | `"2026-05-21T08:01:35Z"` |
| `Field_4_varint` | Integer | **Inserts** - Number of INSERT operations since last update | `150` |
| `Field_5_varint` | Integer | **Updates** - Number of UPDATE operations since last update | `320` |
| `Field_6_varint` | Integer | **Deletes** - Number of DELETE operations since last update | `12` |
| `Field_7_varint` | Integer | **Total Operations** - Sum of inserts + updates + deletes | `482` |

**Enriched Fields:**

| Field | Type | Meaning | Example |
|-------|------|---------|---------|
| `_enriched.entity_display` | String | Entity ID + Name for display | `"2f7b032d \"GSLIBTST.CUSTOMERS\" entity"` |
| `_enriched.source_table` | String | Source table (schema.table) | `"GSLIBTST.CUSTOMERS"` |
| `_enriched.target_table` | String | Target table (schema.table) | `"dbo.CUSTOMERS"` |
| `_enriched.timestamp_human` | String | Human-readable timestamp | `"2026-05-21 08:01:35 UTC"` |
| `_enriched.fields.*` | Object | Field descriptions for each metric | See above |

---

### 2. **EntityStatusMessage** - Entity state changes

**What it represents**: Status change events containing Boolean activity flags for each entity (e.g., active sync, active snapshot migration, or busy/paused state).

**Raw Protobuf Structure (from GlueSync):**
```json
{
  "Field_1_string": "EntityStatusMessage",
  "Field_2_message": {
    "Field_1_message": {
      "Field_1_message": {
        "Field_1_string": "f590ab8c",  // Pipeline ID
        "Field_2_string": "dd7a3d18",  // Entity ID
        "Field_3_varint": 0,           // isMigrationActive (0/1)
        "Field_4_varint": 1,           // isSyncActive (0/1)
        "Field_5_varint": 0,           // isBusy (0/1)
        "Field_6_varint": 0            // snapshotWriteMethod or other field
      }
    }
  }
}
```

**Enriched Output (what browser receives):**
```json
{
  "Field_1_string": "EntityStatusMessage",
  "Field_2_message": {
    "Field_1_message": {
      "Field_1_message": {
        "Field_1_string": "f590ab8c",
        "Field_2_string": "GSLIBTST.CUSTOMERS",  // Enriched Entity Name
        "Field_3_varint": 0,
        "Field_4_varint": 1,
        "Field_5_varint": 0,
        "Field_6_varint": 0,
        "_enriched": {
          "entity_display": "dd7a3d18 \"GSLIBTST.CUSTOMERS\" entity",
          "status_text": "RUNNING",
          "status_description": "Entity is currently running (CDC Sync active)"
        }
      }
    }
  }
}
```

**Status Flag Mapping Rules:**

The status is computed based on the active Boolean flags:
- If `isMigrationActive` (Field_3_varint) is `1` → `MIGRATING` (Snapshotting)
- Else if `isSyncActive` (Field_4_varint) is `1` → `RUNNING` (CDC active)
- Else if `isBusy` (Field_5_varint) is `1` → `PAUSED`
- Otherwise → `STOPPED`

**Enriched Fields:**

| Field | Type | Meaning | Example |
|-------|------|---------|---------|
| `_enriched.entity_display` | String | Entity ID + Name for display | `"dd7a3d18 \"GSLIBTST.CUSTOMERS\" entity"` |
| `_enriched.status_text` | String | Human-readable derived status | `"RUNNING"` |
| `_enriched.status_description` | String | Derived status description | `"Entity is currently running (CDC Sync active)"` |


---

## ID Types Reference

### Pipeline ID
- **Format**: 8-character hex string
- **Example**: `f590ab8c`
- **Usage**: Identifies a replication pipeline (collection of entities)
- **Metadata Source**: `GET /pipelines` → `id` field
- **Display Format**: `f590ab8c "My1st pipeline" pipeline`

### Entity ID
- **Format**: 8-character hex string
- **Example**: `2f7b032d`
- **Usage**: Identifies a specific entity (table replication) within a pipeline
- **Metadata Source**: `GET /pipelines/{id}/entities` → `entityId` field
- **Entity Name**: From `entityName` field (e.g., `"GSLIBTST.CUSTOMERS"`)
- **Display Format**: `2f7b032d "GSLIBTST.CUSTOMERS" entity`

### Agent ID
- **Format**: 8-character hex string
- **Example**: `dfb34af1` (source agent), `063c551e` (target agent)
- **Usage**: Identifies GlueSync agents that perform replication
- **Metadata Source**: `GET /pipelines/{id}/entities` → `agentEntities[].agentId` field

---

## Metadata Lookup Logic

When the backend receives a WebSocket message, it performs a **two-step lookup**:

```python
# Step 1: Try to find by entity NAME (direct match)
meta = metadata_cache.get(raw_entity)

# Step 2: If not found, search by entity ID (reverse lookup)
if not meta:
    for entity_name, entity_meta in metadata_cache.items():
        if entity_meta.get('id') == raw_entity:
            meta = entity_meta
            break
```

**Why this matters**: GlueSync may send entity identifiers as either names or IDs in WebSocket messages. The dual lookup ensures we always resolve to the correct metadata.

**Example Scenario**:
1. WebSocket receives: `Field_1_string = "4bacd683"` (an ID, not a name)
2. Lookup by name `"4bacd683"` → **Not found**
3. Search by ID `"4bacd683"` → **Found!** → Entity name is `"GSLIBTST.THAI_TEST"`
4. Enriched display: `4bacd683 "GSLIBTST.THAI_TEST" entity` ✅

---

## Metadata Example

Sample metadata from GlueSync REST API:

```json
{
  "entityId": "2f7b032d",
  "entityName": "GSLIBTST.CUSTOMERS",
  "agentEntities": [
    {
      "agentType": "SOURCE",
      "table": {
        "schema": "GSLIBTST",
        "name": "CUSTOMERS"
      }
    },
    {
      "agentType": "TARGET",
      "table": {
        "schema": "dbo",
        "name": "CUSTOMERS"
      }
    }
  ]
}
```

This gets cached as:
```python
metadata_cache["GSLIBTST.CUSTOMERS"] = {
    'type': 'entity',
    'name': 'GSLIBTST.CUSTOMERS',
    'id': '2f7b032d',
    'source_table': 'GSLIBTST.CUSTOMERS',
    'target_table': 'dbo.CUSTOMERS'
}
```

---

## Usage Example

### In Browser Console (JavaScript):

```javascript
// Connect to WebSocket
const ws = new WebSocket('ws://192.168.13.53:8000/ws/metrics?pipeline_id=f590ab8c&entities=GSLIBTST.CUSTOMERS,GSLIBTST.THAI_TEST');

ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    
    if (data.Field_1_string === 'MetricsMessage') {
        const inner = data.Field_2_message.Field_1_message.Field_1_message.Field_2_message;
        const enriched = inner._enriched;
        
        console.log(`Entity: ${enriched.entity_display}`);
        console.log(`Source: ${enriched.source_table}`);
        console.log(`Target: ${enriched.target_table}`);
        console.log(`Time: ${enriched.timestamp_human}`);
        console.log(`Inserts: ${enriched.fields.Field_4_varint.value}`);
        console.log(`Updates: ${enriched.fields.Field_5_varint.value}`);
        console.log(`Deletes: ${enriched.fields.Field_6_varint.value}`);
        console.log(`Total: ${enriched.fields.Field_7_varint.value}`);
    }
    
    if (data.Field_1_string === 'EntityStatusMessage') {
        const inner = data.Field_2_message.Field_1_message.Field_1_message;
        const enriched = inner._enriched;
        
        console.log(`Entity: ${enriched.entity_display}`);
        console.log(`Status: ${enriched.status_text} (${enriched.status_description})`);
    }
};
```

---

## Notes

1. **Metadata Cache**: The enrichment uses metadata loaded from GlueSync REST API when the WebSocket connection is established. If an entity is not in the cache, the enriched fields will show `"unknown"` for IDs.

2. **Timestamp Formats**: The system tries to parse timestamps in two formats:
   - ISO 8601: `2026-05-21T08:01:35Z`
   - Epoch milliseconds: `1716278495000`
   
   If parsing fails, the raw value is used.

3. **Backward Compatibility**: The enriched data is added as a `_enriched` field, so existing code that uses the raw fields continues to work.

4. **Performance**: Metadata is fetched once per WebSocket connection and cached. It does not re-fetch on every message.

---

## Troubleshooting

### Missing Enriched Data
If `_enriched` field is missing:
- Check if metadata loading failed (look for `[ws] Warning: Could not load metadata` in container logs)
- Verify GlueSync credentials are set in `.env` file
- Check if entity name matches exactly between WebSocket subscription and GlueSync metadata

**Debug command**:
```bash
podman logs replica-mon 2>&1 | grep -i "ws\|metadata"
```

### Entity Shows ID Instead of Name
If display shows `4bacd683 "4bacd683" entity` (ID repeated):
- Metadata cache may not have loaded properly
- Entity ID `4bacd683` not found in REST API response
- Check if the entity exists in pipeline: `GET /pipelines/{id}/entities`

**Debug command**:
```bash
# Check what entities are in the pipeline
curl -s http://localhost:8000/api/pipelines/f590ab8c/entities | python3 -m json.tool | grep -E "entityName|entityId"
```

### Incorrect Status Codes
If status shows as `UNKNOWN(X)`:
- GlueSync may be using a new status code not in our mapping
- Check container logs for the raw status value
- Update the `status_map` in `enrich_with_metadata()` function

**Current status mapping**:
```python
status_map = {
    0: 'STOPPED',
    1: 'RUNNING',
    2: 'PAUSED',
    3: 'ERROR'
}
```

### Metadata Cache Not Updating
- Metadata is fetched **once per WebSocket connection**
- If you add new entities, **reconnect the WebSocket** to refresh cache
- Cache does NOT auto-refresh during connection

---

## Implementation Details

### Backend Function: `enrich_with_metadata()`

Location: `/app/replica-mon/backend/main.py`

**Responsibilities**:
1. Parse raw Protobuf message structure
2. Extract entity identifier (name or ID)
3. Perform dual lookup (name → ID fallback)
4. Add `_enriched` field with human-readable data
5. Convert timestamps to readable format
6. Map status codes to text

**Key Fields Added**:
```python
inner['_enriched'] = {
    'entity_display': '2f7b032d "GSLIBTST.CUSTOMERS" entity',  # Main display string
    'entity_name': 'GSLIBTST.CUSTOMERS',                       # Resolved name
    'entity_id': '2f7b032d',                                   # Resolved ID
    'source_table': 'GSLIBTST.CUSTOMERS',                      # From metadata
    'target_table': 'dbo.CUSTOMERS',                           # From metadata
    'fields': {...},                                           # Field descriptions
    'timestamp_human': '2026-05-21 08:01:35 UTC'               # Formatted timestamp
}
```

---
