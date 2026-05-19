# Replica-Mon Metrics Architecture

## Overview

Two complementary data sources provide different levels of insight:

| Source | What it provides | Retention |
|---|---|---|
| **Prometheus** (`gluesync_total_count`) | Total rows replicated per entity — all ops combined | **5 days** (configurable) |
| **WebSocket stream** (`MetricsMessage`) | Per-operation breakdown (INS / UPD / DEL) — live stream | **Runtime only** unless stored |
| **SQLite time-series store** (our store) | Accumulated I/U/D breakdown with timestamps | **Configurable, default 30 days** |

---

## Source 1: Prometheus — Total Row Counts

### Available GlueSync Metrics

| Metric | Description | Use Case |
|---|---|---|
| `gluesync_total_count` | Cumulative rows replicated since entity start | ✅ "Since midnight, how many rows?" |
| `gluesync_last_rows_count` | Rows in the **most recent batch** only | ✅ "What's the last batch size?" |
| `gluesync_sma_count` | Simple Moving Average of recent batch sizes | ✅ "What's the average throughput trend?" |
| `gluesync_last_end_to_end_time` | End-to-end latency of last processing cycle (ms) | ✅ "What's the current replication lag?" |
| `gluesync_last_read_time` | Time spent reading from source (ms) | ✅ Performance profiling |
| `gluesync_last_write_time` | Time spent writing to target (ms) | ✅ Performance profiling |
| `gluesync_total_size_bytes` | Total bytes replicated since start | ✅ Data volume tracking |
| `gluesync_total_snapshot_count` | Total snapshot rows replicated | ✅ Initial load tracking |

### Metric Labels (per entity)

Every GlueSync metric carries these labels for filtering:
```
entityId       → "2f7b032d"
entityName     → "GSLIBTST.CUSTOMERS"
pipelineId     → "f590ab8c"
pipelineName   → "My1st pipeline"
sourceAgentId  → "dfb34af1"
targetAgentId  → "063c551e"
instance       → "10.88.0.40:1717"
job            → "gluesync-core-hub"
```

### PromQL Queries

#### Total rows in a time window
```promql
# Rows replicated in the last 8 hours (since ~midnight)
increase(gluesync_total_count{entityName="GSLIBTST.CUSTOMERS"}[8h])

# All entities — rows in last 1 hour
increase(gluesync_total_count[1h])

# All entities — rows in last 24 hours
increase(gluesync_total_count[24h])
```

#### Live throughput
```promql
# Current rows/second per entity (5-minute moving average)
rate(gluesync_total_count[5m])

# Peak throughput in the last hour
max_over_time(rate(gluesync_total_count[5m])[1h:1m])
```

#### Latency / performance
```promql
# Current end-to-end replication latency (ms)
gluesync_last_end_to_end_time

# Average latency over the last 30 minutes
avg_over_time(gluesync_last_end_to_end_time[30m])
```

### How to Query via API

```bash
# Instant query (current value)
curl 'http://localhost:9090/prometheus/api/v1/query?query=increase(gluesync_total_count[8h])'

# Range query (time-series for graphing — last 24h at 5-min resolution)
curl 'http://localhost:9090/prometheus/api/v1/query_range
  ?query=increase(gluesync_total_count[5m])
  &start=2026-05-18T00:00:00Z
  &end=2026-05-18T23:59:59Z
  &step=300'
```

### ⚠️ Cautions

> **Counter Reset on Entity Restart**
> `gluesync_total_count` resets to 0 whenever an entity is stopped and restarted.
> PromQL's `increase()` handles this automatically with counter-reset detection,
> but if an entity is restarted mid-window, the `increase()` result will be lower
> than the true total. Use with awareness.

> **Prometheus Retention = 5 days**
> Configured via `--storage.tsdb.retention.time=5d` in `podman-compose.yml`.
> Queries beyond 5 days will return no data.
> To extend: add `--storage.tsdb.retention.time=30d` to the Prometheus command args.

> **No I/U/D Breakdown**
> Prometheus only exposes the **combined** row count. It cannot tell you how many
> were inserts vs updates vs deletes. Use the WebSocket → SQLite store for that.

> **IP Hardcoded in prometheus.yml**
> Podman v3.4.4 does not support container DNS in custom networks.
> The Core Hub IP must be patched after each stack restart.
> `run-podman.sh` does this automatically via `podman inspect`.

---

## Source 2: WebSocket Stream → SQLite Time-Series Store

### What the WebSocket Provides

GlueSync Core Hub pushes binary Protobuf messages via WebSocket at:
```
wss://<host>:1717/ui
```

Our FastAPI backend (`/ws/metrics`) decodes these and forwards clean JSON.

**`MetricsMessage` payload** (after Protobuf decoding):
```json
{
  "Field_1_string": "MetricsMessage",
  "Field_2_message": {
    "Field_1_message": {
      "Field_1_message": {
        "Field_2_message": {
          "Field_1_string": "GSLIBTST.CUSTOMERS",
          "Field_2_varint": 1747123456,
          "Field_4_varint": 1500,
          "Field_5_varint": 230,
          "Field_6_varint": 45,
          "Field_7_varint": 1775
        }
      }
    }
  }
}
```

| Field | Meaning |
|---|---|
| `Field_1_string` | Entity name |
| `Field_2_varint` | Timestamp (epoch seconds) |
| `Field_4_varint` | **Inserts** count (cumulative) |
| `Field_5_varint` | **Updates** count (cumulative) |
| `Field_6_varint` | **Deletes** count (cumulative) |
| `Field_7_varint` | **Total ops** count (cumulative) |

### SQLite Schema (`metrics/ws_metrics.db`)

```sql
CREATE TABLE ws_metrics (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,           -- ISO timestamp when we received it
    pipeline_id  TEXT NOT NULL,
    entity_name  TEXT NOT NULL,
    inserts      INTEGER DEFAULT 0,       -- cumulative at time of capture
    updates      INTEGER DEFAULT 0,
    deletes      INTEGER DEFAULT 0,
    total_ops    INTEGER DEFAULT 0
);
CREATE INDEX idx_entity_time ON ws_metrics(entity_name, captured_at);
```

### Query Examples

```sql
-- I/U/D breakdown for CUSTOMERS since midnight
SELECT
    entity_name,
    MAX(inserts) - MIN(inserts) AS new_inserts,
    MAX(updates) - MIN(updates) AS new_updates,
    MAX(deletes) - MIN(deletes) AS new_deletes,
    MAX(total_ops) - MIN(total_ops) AS new_total
FROM ws_metrics
WHERE entity_name = 'GSLIBTST.CUSTOMERS'
  AND captured_at >= datetime('now', 'localtime', 'start of day')
GROUP BY entity_name;

-- Hourly throughput breakdown (last 24 hours)
SELECT
    strftime('%Y-%m-%d %H:00', captured_at) AS hour,
    entity_name,
    MAX(total_ops) - MIN(total_ops) AS ops_in_hour
FROM ws_metrics
WHERE captured_at >= datetime('now', '-24 hours')
GROUP BY hour, entity_name
ORDER BY hour, entity_name;
```

### ⚠️ Cautions

> **Cumulative values, not deltas**
> `Field_4_varint` (inserts) is a running total, not "inserts this batch".
> Always use `MAX - MIN` over a time window to get the delta.

> **Resets on entity restart**
> Same as Prometheus — values reset to 0 when an entity stops/restarts.
> A sudden drop in value signals a reset, not a deletion of rows.

> **Only stored while backend is running**
> If `replica-mon` container is down, no data is accumulated.
> Gap detection: if `MAX(captured_at) - MIN(captured_at) >> expected_coverage`,
> there was a gap in collection.

> **WebSocket reconnects on disconnect**
> The backend auto-reconnects every 5 seconds if the stream drops.

---

## REST API Endpoints (FastAPI Backend)

### Available Now
| Endpoint | Description |
|---|---|
| `GET /api/pipelines` | List all pipelines |
| `GET /api/pipelines/{id}/entities` | List entities for a pipeline |
| `POST /api/pipelines/{id}/entities/{name}/start` | Start entity replication |
| `POST /api/pipelines/{id}/entities/{name}/stop` | Stop entity replication |
| `WS /ws/metrics` | Live decoded metrics stream |

### Planned (implementing now)
| Endpoint | Description |
|---|---|
| `GET /api/metrics/{pipeline_id}/{entity_name}` | I/U/D breakdown for time window |
| `GET /api/metrics/{pipeline_id}` | Summary for all entities in pipeline |
| `GET /api/prometheus/query` | Proxy PromQL query (total counts, throughput) |

---

## Decision Matrix: Which Source to Use

| Question | Source | Query |
|---|---|---|
| Total rows replicated since midnight | Prometheus | `increase(gluesync_total_count[8h])` |
| Total rows in last 1 hour | Prometheus | `increase(gluesync_total_count[1h])` |
| Live rows/sec throughput | Prometheus | `rate(gluesync_total_count[5m])` |
| End-to-end replication latency | Prometheus | `gluesync_last_end_to_end_time` |
| INSERT count since midnight | SQLite store | `MAX(inserts) - MIN(inserts)` since 00:00 |
| UPDATE count since midnight | SQLite store | `MAX(updates) - MIN(updates)` since 00:00 |
| DELETE count since midnight | SQLite store | `MAX(deletes) - MIN(deletes)` since 00:00 |
| Hourly I/U/D trend chart | SQLite store | GROUP BY hour |
| Source vs target row validation | SQLite (future) + qadmcli (on-demand) | Cross-reference |
