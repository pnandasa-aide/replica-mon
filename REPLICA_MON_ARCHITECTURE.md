# ReplicaMon System Architecture & Reference Guide

This document provides a comprehensive overview of the **ReplicaMon** monitoring and verification architecture, detailing how it integrates with **GlueSync**, **qadmcli**, and **replica-cli**.

---

## 1. System Architecture Overview

ReplicaMon is a dual-purpose tool providing both **real-time operational monitoring** and **on-demand data verification/reconciliation** for GlueSync replication pipelines between AS400 (source) and MSSQL (target).

### System Data Flows

```mermaid
flowchart TD
    subgraph Core Replication Loop
        AS400[(AS400 Source\nDB2 for i)] <-->|CDC Journaling| GS[GlueSync CoreHub]
        GS <-->|Replication Stream| MSSQL[(MSSQL Target)]
    end

    subgraph Monitor Stack (replica-mon)
        Backend[FastAPI Backend\nmain.py]
        SQLite[(SQLite Time-Series\nws_metrics.db)]
        PromProxy[Prometheus Proxy]
        VerifyWorker[Verification Worker\nBackground Thread]
        Dashboard[Web Dashboard\ndashboard_mockup.html]
    end

    subgraph Sibling Projects
        SDK[replica_msdk\nShared Client]
        QCLI[qadmcli\nAS400/MSSQL Client]
        RCLI[replica-cli\nGlueSync Controller]
    end

    subgraph Monitoring Infrastructure
        Prom[Prometheus Server]
    end

    %% Metrics Gathering
    GS -.->|Prometheus Metrics| Prom
    Prom -->|Scraped Metrics| PromProxy
    Backend -->|PromQL query| PromProxy
    
    %% WebSocket Live Telemetry
    GS ===|wss://.../ui\nBinary Protobuf Stream| SDK
    SDK -->|Recurse & Decode| Backend
    Backend -->|Live JSON| Dashboard
    Backend -->|Persist Running Totals| SQLite

    %% On-Demand Verification
    Dashboard -->|POST /api/verify/run| VerifyWorker
    VerifyWorker -->|Direct pyodbc count| MSSQL
    VerifyWorker -->|AS400ConnectionManager\nJDBC / jt400.jar| QCLI
    QCLI -->|jaydebeapi query| AS400

    %% Auto-Discovery
    Backend -.->|Auto-discover config| RCLI
    Dashboard -->|UI Interactions| Backend
```

### Core Architecture Components

| Component | Responsibility | Tech Stack |
|---|---|---|
| **Web Dashboard** | Displays live metrics, pipeline states, active entities, real-time WebSocket metrics, historic trend charts (via SQLite), and triggers verification. | HTML5, Vanilla CSS, JS (Chart.js / WebSocket) |
| **FastAPI Backend** | Exposes REST APIs, proxies Prometheus queries, hosts the WS pipeline, and runs verification. | Python, FastAPI, Uvicorn |
| **SQLite Time-Series Store** | Keeps a rolling 30-day index of operation-level changes (Inserts / Updates / Deletes). | SQLite (`ws_metrics.db`) |
| **Shared SDK (`replica_msdk`)** | Standardizes GlueSync CoreHub REST API connections and WebSocket subscriptions. | Python, Requests, Websockets |
| **Protobuf Parser** | Parses deep binary Protobuf packets recursively without compilation dependency. | Python wire-format parser |
| **Verification Worker** | Orchestrates asynchronous database counts and calculates exact row deltas. | Python Multi-threading |
| **qadmcli Engine** | Provides database connectivity, Change Tracking controls, and host-level utilities. | Python, JayDeBeApi, PyODBC, JDBC (`jt400.jar`) |

---

## 2. Real-Time Telemetry & Caching

### Source A: The WebSocket Protobuf Stream

GlueSync streams live statistics from its `/ui` endpoint. However, it streams them as **highly nested binary Protocol Buffers (Protobuf)**, meaning field names are stripped out and numbers are compressed into binary "varints" (Variable-Length Integers).

1. **Subscription:** The `replica_msdk` uses `PUT /ui/entities-metrics-subscription` to register the pipeline and active entities using Bearer token authentication.
2. **Decoupled Parser:** A recursive zero-dependency Python parser (`parse_protobuf()`) unpacks nested tags by reading wire-types:
   - **Wire Type 0 (Varint):** Decodes integer metrics.
   - **Wire Type 2 (Length-delimited):** Decodes UTF-8 strings or recursively parses nested control blocks if raw bytes are found.
3. **Metrics Storage:** Once a `MetricsMessage` is decoded into clean JSON, the running totals (Inserts, Updates, Deletes) are extracted and saved into the SQLite database.

### Source B: SQLite Time-Series Database

The SQLite database (`replica-mon/metrics/ws_metrics.db`) logs rolling snapshots of the WebSocket telemetry. Because metrics are **cumulative running totals**, the backend implements a **MAX-MIN Delta Query Pattern** over selected windows:

```sql
SELECT
    entity_name,
    MAX(inserts) - MIN(inserts) AS new_inserts,
    MAX(updates) - MIN(updates) AS new_updates,
    MAX(deletes) - MIN(deletes) AS new_deletes,
    MAX(total_ops) - MIN(total_ops) AS new_total
FROM ws_metrics
WHERE entity_name = :entity_name
  AND captured_at >= datetime('now', 'localtime', :window_offset);
```
> [!NOTE]
> This pattern handles counter resets (e.g. if the GlueSync pipeline is restarted) gracefully using a `max(0, ...)` constraint in the python layer.

---

## 3. How `qadmcli` is Reused

`qadmcli` is a powerful database management tool shared across our replication projects. Rather than duplicating its complex database connection algorithms, ReplicaMon **reuses it in two distinct modes**:

### Mode 1: Direct Python Package Import (Inside Container)

When building the `replica-mon` container image, the `qadmcli` project is copied in and installed globally as a Python package. This enables high-performance direct execution in python without subprocess overhead.

1. **Dependency Installation (`Containerfile`):**
   ```dockerfile
   COPY qadmcli /opt/qadmcli
   RUN cd /opt/qadmcli && pip install .
   ```
2. **Importing the Engine (`main.py`):**
   ```python
   from qadmcli.config import load_config
   from qadmcli.db.connection import AS400ConnectionManager
   ```
3. **Execution:** The verification task calls `AS400ConnectionManager` directly. It boots the Java Virtual Machine (`JPype`), initializes the JT400 JDBC driver (`jt400.jar`), connects to the database, executes `SELECT COUNT(*)`, and returns the results synchronously.

### Mode 2: Shell/Subprocess Wrapper (Host/CLI Scripts)

For command-line checks (e.g., `compare.py`, `monitor.py` or scheduled cron jobs) outside the web server:

1. **Path Detection:** The Python scripts automatically detect the location of `qadmcli.sh` in the sibling directory (`../qadmcli/qadmcli.sh`) or respect the `QADMCLI_PATH` environment variable.
2. **Subprocess Calls:** The script executes `qadmcli.sh` inside a Python `subprocess.run` session, invoking:
   - `qadmcli journal entries` to retrieve AS400 entries.
   - `qadmcli mssql ct changes` to read MSSQL Change Tracking.
   - `qadmcli mssql ct status` to verify table change log state.
3. **JSON Interception:** The output is intercepted, ANSI terminal escapes are stripped, the JSON block is isolated and parsed into Python dict structures:

```python
cmd = ["../qadmcli/qadmcli.sh", "journal", "entries", "-t", table, "-l", library, "--format", "json"]
result = subprocess.run(cmd, capture_output=True, text=True)
# Strips ANSI escapes and extracts json dictionaries
```

---

## 4. Timezone-Aware Reconciliation & Fallbacks

Reconciling row changes requires careful timezone awareness due to different clock scales:
- **AS400 Journals:** Log timestamps in **UTC+0**.
- **MSSQL Databases:** Configured in **UTC+7** (Asia/Bangkok).

1. **Time Normalization:** Before querying source and target history, timestamps are converted dynamically:
   - When checking changes "since midnight", the user's `UTC+7` query timestamp is shifted to its `UTC+0` equivalent to query the AS400 journal.
2. **Graceful Change Tracking Fallback:** If Change Tracking is not enabled on a target table, the comparison script automatically detects it (`qadmcli mssql ct status`) and downgrades to a simple **COUNT(*) comparison** with warning explanations, preventing the tool from failing.

---

## 5. Asynchronous Data Verification & Reconciliation Engine

To resolve potential UI timeouts when verifying very large tables, ReplicaMon uses a polling-based asynchronous verification pipeline.

### The Verification Workflow

1. **Triggering the Job:**
   - The user triggers a reconciliation job via the Dashboard, which issues a `POST /api/verify/{pipeline_id}/run`.
   - The backend discovers active tables/entities dynamically from GlueSync, instantiates a background thread (`_verify_worker`), and immediately returns a job ticket with status `started` and a polling URL (`/api/verify/{pipeline_id}/results`).

2. **The Background Worker (`_verify_worker`):**
   - The worker iterates through each table pair in sequence to calculate row counts.
   - **AS400 Source Counting:** Imports the `qadmcli` module directly to use its JDBC connection capability (`AS400ConnectionManager`). It automatically reads the credentials from the parent `.env` file and executes a direct `SELECT COUNT(*)` on the DB2 library/table.
   - **MSSQL Target Counting:** Uses `pyodbc` to execute a direct query on the SQL Server instance. It also attempts to retrieve the latest update timestamp by scanning for common date/time columns (e.g., `LastUpdate`, `UpdatedAt`).
   - The worker updates a thread-safe, in-memory job store (`_verify_jobs`) incrementally as each table is counted.

3. **Incremental UI Polling:**
   - The Web Dashboard polls `GET /api/verify/{pipeline_id}/results` every 2 seconds.
   - The response includes the current `done_count`, the total table list, and the computed deltas for completed tables. This provides immediate, real-time feedback to the user as reconciliation progress is made.

---

## 6. How to Update ReplicaMon when Sibling Projects Update

If you make modifications to the sibling utilities (`qadmcli`, `replica-cli`, or `replica_msdk`), you must ensure `replica-mon` gets these updates. How to do this depends on how you are running the project:

### Scenario A: Running in Container (Podman / Docker)

Since the container compiles dependencies into the image at build time, **any change in a sibling project requires a rebuild of the `replica-mon` image.**

1. **Rebuild via Helper Script (Recommended):**
   The `replica-mon.sh` wrapper contains a built-in flag to automatically rebuild the image using the parent directory as the build context:
   ```bash
   cd ~/prefix/replica-mon
   ./replica-mon.sh --rebuild
   ```
2. **Manual Compose Rebuild:**
   If running via Podman Compose:
   ```bash
   podman-compose build --no-cache
   podman-compose up -d
   ```

> [!IMPORTANT]
> The build context for the `replica-mon` image is the **parent directory** (`../`). This is necessary so the builder can access sibling folders like `qadmcli/` during compilation. If you build from the `replica-mon` folder directly, the build will fail because it cannot find the sibling folders.

### Scenario B: Running Scripts Directly on Host

If you are running the command-line scripts (`compare.py` or `monitor.py`) directly on the host (outside Podman):

* **No action required!** Because the host scripts dynamically locate and invoke `../qadmcli/qadmcli.sh` or look up sibling Python imports at runtime, any code update inside `qadmcli` or `replica-cli` is **instantly reflected** in your next script execution.

---

## 7. Directory Map & Quick Reference

```
_qoder/

├── .env                       <-- Core Shared Credentials (DBs, IPs, passwords)
├── qadmcli/                   <-- AS400 / MSSQL Administrative tool
│   ├── config/connection.yaml <-- Connection configs used by Direct JDBC
│   └── qadmcli.sh             <-- CLI helper executed in Subprocess mode
├── replica-cli/               <-- GlueSync REST client & provisioning
│   └── config.json            <-- Target fallback database configurations
├── replica_msdk/              <-- Shared GlueSync client SDK & WS Decoder
└── replica-mon/               <-- Dashboard and Verification Engine
    ├── Containerfile          <-- Dockerfile linking all components
    ├── replica-mon.sh         <-- Main execution script
    ├── compare.py             <-- Row level timezone-aware comparator
    ├── monitor.py             <-- Automated pipeline checking daemon
    ├── backend/
    │   └── main.py            <-- FastAPI server, SQLite logger & WS worker
    └── cache/                 <-- SQLite journal caches (rolling retention)
```
