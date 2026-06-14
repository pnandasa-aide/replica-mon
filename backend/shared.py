import os
import sys
import sqlite3
import threading
import logging
import json
from typing import Dict, Any

# Ensure replica-msdk can be imported
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from replica_msdk import GlueSyncClient, parse_protobuf

logger = logging.getLogger("replica_mon.shared")

GLUESYNC_URL = os.getenv("GLUESYNC_HOST", "https://localhost:1717")
ADMIN_PASS = os.getenv("GLUESYNC_ADMIN_PASSWORD") or os.getenv("ADMIN_PASS")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")

# SQLite time-series store — use absolute path to avoid --app-dir __file__ resolution issues
_base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
METRICS_DB_PATH = os.getenv("METRICS_DB_PATH") or os.path.join(_base_dir, "metrics/ws_metrics.db")
REPORTS_DIR = os.getenv("REPORTS_DIR") or os.path.join(_base_dir, "compare/reports")
CACHE_DIR = os.getenv("CACHE_DIR") or os.path.join(_base_dir, "cache")

# Thread-local SQLite connections
_db_local = threading.local()

def get_db():
    if not hasattr(_db_local, 'conn') or _db_local.conn is None:
        _db_local.conn = sqlite3.connect(METRICS_DB_PATH, check_same_thread=False)
        _db_local.conn.row_factory = sqlite3.Row
    return _db_local.conn

# Global token cache for proxy endpoint
_gluesync_token = None
_gluesync_token_expiry = 0

def get_gluesync_token() -> str:
    """Get or refresh GlueSync Bearer token."""
    global _gluesync_token, _gluesync_token_expiry
    import time
    import httpx
    if _gluesync_token and time.time() < _gluesync_token_expiry:
        return _gluesync_token
    try:
        with httpx.Client(verify=False, timeout=10.0) as client:
            resp = client.post(
                f"{GLUESYNC_URL}/authentication/login",
                json={"username": "admin", "password": ADMIN_PASS}
            )
            resp.raise_for_status()
            data = resp.json()
            _gluesync_token = data.get("apiToken")
            _gluesync_token_expiry = time.time() + 300  # 5 minutes
            print(f"[auth] Got new GlueSync token", flush=True)
            return _gluesync_token
    except Exception as e:
        print(f"[auth] Failed to get GlueSync token: {e}", flush=True)
        raise

def get_gluesync_client() -> GlueSyncClient:
    if not ADMIN_PASS:
        raise Exception("GLUESYNC_ADMIN_PASSWORD environment variable is required")
    return GlueSyncClient(GLUESYNC_URL, "admin", ADMIN_PASS, verify_ssl=False)

def store_metrics(pipeline_id: str, entity_name: str, inserts: int, updates: int, deletes: int, total_ops: int):
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO ws_metrics (captured_at, pipeline_id, entity_name, inserts, updates, deletes, total_ops)
               VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?)""",
            (pipeline_id, entity_name, inserts, updates, deletes, total_ops)
        )
        conn.commit()
    except Exception as e:
        print(f"[metrics-db] Error storing metrics: {e}")

def store_entity_status(pipeline_id: str, entity_name: str, status: str):
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO entity_status_cache (pipeline_id, entity_name, status, updated_at)
               VALUES (?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(pipeline_id, entity_name) DO UPDATE SET
               status=excluded.status, updated_at=excluded.updated_at""",
            (pipeline_id, entity_name, status)
        )
        conn.commit()
    except Exception as e:
        print(f"[metrics-db] Error caching entity status: {e}")

def get_all_cached_statuses(pipeline_id: str) -> dict:
    try:
        conn = get_db()
        rows = conn.execute(
            "SELECT entity_name, status FROM entity_status_cache WHERE pipeline_id = ?",
            (pipeline_id,)
        ).fetchall()
        return {r["entity_name"]: r["status"] for r in rows}
    except Exception as e:
        print(f"[metrics-db] Error reading cached entity statuses: {e}")
        return {}

def store_agent_health(pipeline_id: str, agent_id: str, agent_type: str,
                       connection_status: str, health_status: str,
                       connected_db_host: str = None, connected_db_name: str = None,
                       connection_error: str = None):
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO agent_health_cache
               (pipeline_id, agent_id, agent_type, connection_status, health_status,
                connected_db_host, connected_db_name, connection_error, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(pipeline_id, agent_id) DO UPDATE SET
               agent_type=excluded.agent_type,
               connection_status=excluded.connection_status,
               health_status=excluded.health_status,
               connected_db_host=excluded.connected_db_host,
               connected_db_name=excluded.connected_db_name,
               connection_error=excluded.connection_error,
               updated_at=excluded.updated_at""",
            (pipeline_id, agent_id, agent_type, connection_status, health_status,
             connected_db_host, connected_db_name, connection_error)
        )
        conn.commit()
    except Exception as e:
        print(f"[metrics-db] Error caching agent health: {e}")

def get_all_agent_health(pipeline_id: str) -> list:
    try:
        conn = get_db()
        rows = conn.execute(
            """SELECT agent_id, agent_type, connection_status, health_status,
                      connected_db_host, connected_db_name, connection_error, updated_at
               FROM agent_health_cache WHERE pipeline_id = ?""",
            (pipeline_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[metrics-db] Error reading agent health: {e}")
        return []

def init_comparison_tables():
    """Create comparison_cache and comparison_checkpoints tables."""
    try:
        conn = get_db()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS comparison_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pipeline_id TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                library TEXT,
                source_table TEXT,
                target_table TEXT,
                source_changes INTEGER DEFAULT 0,
                target_changes INTEGER DEFAULT 0,
                source_inserts INTEGER DEFAULT 0,
                source_updates INTEGER DEFAULT 0,
                source_deletes INTEGER DEFAULT 0,
                target_inserts INTEGER DEFAULT 0,
                target_updates INTEGER DEFAULT 0,
                target_deletes INTEGER DEFAULT 0,
                gap INTEGER DEFAULT 0,
                gap_trend TEXT DEFAULT 'stable',
                consecutive_cycles_gap_positive INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                cycle_start TEXT,
                cycle_end TEXT,
                captured_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(pipeline_id, entity_name, cycle_start)
            );
            CREATE TABLE IF NOT EXISTS comparison_checkpoints (
                pipeline_id TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                source_table TEXT,
                target_table TEXT,
                last_journal_seq INTEGER DEFAULT 0,
                last_ct_version INTEGER DEFAULT 0,
                last_verified_at TEXT,
                PRIMARY KEY (pipeline_id, entity_name)
            );
        """)
        conn.commit()
        print("[db] Comparison tables initialized", flush=True)
    except Exception as e:
        print(f"[db] Error initializing comparison tables: {e}", flush=True)


def _load_replica_cli_config() -> dict:
    candidates = [
        os.path.join(os.path.dirname(__file__), "../../../replica-cli/config.json"),
        "/app/replica-cli/config.json",
    ]
    for path in candidates:
        try:
            with open(os.path.normpath(path)) as f:
                return json.load(f)
        except Exception:
            continue
    return {}

def _count_mssql(schema: str, table: str):
    import pyodbc
    cfg = _load_replica_cli_config()
    tgt_conn = cfg.get("target_agent", {}).get("connection", {})
    server   = os.getenv("MSSQL_HOST")   or tgt_conn.get("host", "")
    database = os.getenv("MSSQL_DATABASE") or tgt_conn.get("database_name", "")
    user     = os.getenv("MSSQL_USER", "")
    password = os.getenv("MSSQL_PASSWORD", "")
    driver   = os.getenv("MSSQL_DRIVER", "ODBC Driver 18 for SQL Server")
    if not server or not database or not user:
        raise ValueError(
            f"Missing MSSQL config: HOST='{server}' DB='{database}' USER='{user}'"
        )
    conn_str = (f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};"
                f"UID={user};PWD={password};TrustServerCertificate=yes;Encrypt=yes;")
    print(f"[verify] Connecting: SERVER={server} DB={database}", flush=True)
    conn = pyodbc.connect(conn_str, timeout=5)
    cursor = conn.cursor()
    full_table = f"[{schema}].[{table}]"
    cursor.execute(f"SELECT COUNT(*) FROM {full_table}")
    count = cursor.fetchone()[0]
    print(f"[verify]   {full_table} count={count}", flush=True)
    last_ts = None
    for ts_col in ["LastUpdate", "last_update", "UPDATED_AT", "UpdatedAt", "CREATED_AT"]:
        try:
            cursor.execute(f"SELECT MAX([{ts_col}]) FROM {full_table}")
            row = cursor.fetchone()
            if row and row[0]:
                last_ts = str(row[0])
                break
        except Exception:
            continue
    conn.close()
    return count, last_ts

def _count_oracle(schema: str, table: str, pipeline_id: str = None, agent_id: str = None) -> tuple:
    import oracledb
    host = os.getenv("ORACLE_HOST")
    port = os.getenv("ORACLE_PORT", "1521")
    service_name = os.getenv("ORACLE_SERVICE_NAME")
    user = os.getenv("ORACLE_USER")
    password = os.getenv("ORACLE_PASSWORD")
    
    if not (host and user and password) and pipeline_id and agent_id:
        try:
            client = get_gluesync_client()
            agent = client.get_agent(pipeline_id, agent_id)
            if agent:
                creds = agent.get("hostCredentials", {})
                host = host or creds.get("host")
                port = port or str(creds.get("port") or "1521")
                service_name = service_name or creds.get("databaseName") or creds.get("connectionName")
                user = user or creds.get("username")
                # Note: password is hidden in GlueSync API (must be provided via env in production)
        except Exception as e:
            logger.warning(f"[verify] Failed to load Oracle credentials from GlueSync API: {e}")

    if not host or not user or not password:
        raise ValueError(
            f"Missing Oracle configuration (HOST='{host}', USER='{user}'). "
            f"Please set ORACLE_HOST, ORACLE_USER, and ORACLE_PASSWORD in .env."
        )
    
    try:
        port_int = int(port)
    except ValueError:
        port_int = 1521

    print(f"[verify] Connecting to Oracle: host={host} port={port_int} service={service_name} user={user}", flush=True)
    conn = oracledb.connect(
        user=user,
        password=password,
        host=host,
        port=port_int,
        service_name=service_name
    )
    cursor = conn.cursor()
    # Oracle schemas and tables are usually case-sensitive when quoted, default to uppercase
    full_table = f'"{schema.upper()}"."{table.upper()}"'
    cursor.execute(f"SELECT COUNT(*) FROM {full_table}")
    count = cursor.fetchone()[0]
    print(f"[verify]   Oracle {full_table} count={count}", flush=True)

    last_ts = None
    for ts_col in ["LastUpdate", "last_update", "UPDATED_AT", "UpdatedAt", "CREATED_AT", "CREATED_DATE"]:
        try:
            cursor.execute(f"SELECT TO_CHAR(MAX({ts_col}), 'YYYY-MM-DD HH24:MI:SS') FROM {full_table}")
            row = cursor.fetchone()
            if row and row[0]:
                last_ts = str(row[0])
                break
        except Exception:
            continue
            
    cursor.close()
    conn.close()
    return count, last_ts

def _get_qadmcli_config_path() -> str:
    candidates = [
        "/app/qadmcli/config/connection.yaml",
        "/opt/qadmcli/config/connection.yaml",
        os.path.normpath(os.path.join(os.path.dirname(__file__), "../../../qadmcli/config/connection.yaml")),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ""

def _count_as400(library: str, table: str) -> tuple:
    import sys
    sys.stderr.write(f"[verify] AS400 → Starting count for {library}.{table}\n")
    sys.stderr.flush()
    logger.info(f"[verify] AS400 → Starting count for {library}.{table}")
    try:
        from qadmcli.config import load_config
        from qadmcli.db.connection import AS400ConnectionManager
        from pathlib import Path
        config_path = _get_qadmcli_config_path()
        sys.stderr.write(f"[verify] AS400 → Config path: {config_path}\n")
        sys.stderr.flush()
        logger.info(f"[verify] AS400 → Config path: {config_path}")
        if not config_path:
            raise FileNotFoundError("qadmcli connection.yaml not found")
        config = load_config(Path(config_path))
        sys.stderr.write(f"[verify] AS400 → Config loaded: host={config.as400.host}\n")
        sys.stderr.flush()
        logger.info(f"[verify] AS400 → Config loaded: host={config.as400.host}")
        as400_user = os.getenv("AS400_USER")
        as400_pass = os.getenv("AS400_PASSWORD")
        if as400_user or as400_pass:
            config.as400 = config.as400.copy_with_overrides(
                user=as400_user or config.as400.user,
                password=as400_pass or config.as400.password,
            )
            sys.stderr.write(f"[verify] AS400 → Credentials overridden from env\n")
            sys.stderr.flush()
            logger.info(f"[verify] AS400 → Credentials overridden from env")
        sys.stderr.write(f"[verify] AS400 Connecting: {config.as400.host} {library}.{table}\n")
        sys.stderr.flush()
        logger.info(f"[verify] AS400 Connecting: {config.as400.host} {library}.{table}")
        with AS400ConnectionManager(config) as conn:
            sys.stderr.write(f"[verify] AS400 → Connected, executing COUNT query...\n")
            sys.stderr.flush()
            logger.info(f"[verify] AS400 → Connected, executing COUNT query...")
            cursor = conn.execute(f"SELECT COUNT(*) FROM {library}.{table}")
            row = cursor.fetchone()
            count = int(row[0]) if row else 0
            cursor.close()
            sys.stderr.write(f"[verify] AS400 → Query result: {count}\n")
            sys.stderr.flush()
            logger.info(f"[verify] AS400 → Query result: {count}")
        sys.stderr.write(f"[verify]   AS400 {library}.{table} count={count}\n")
        sys.stderr.flush()
        logger.info(f"[verify]   AS400 {library}.{table} count={count}")
        return count, None
    except Exception as e:
        import traceback
        sys.stderr.write(f"[verify] AS400 ✗ Exception: {e}\n")
        sys.stderr.write(f"[verify] AS400 Traceback: {traceback.format_exc()}\n")
        sys.stderr.flush()
        logger.error(f"[verify] AS400 ✗ Exception: {e}")
        logger.error(f"[verify] AS400 Traceback: {traceback.format_exc()}")
        raise RuntimeError(str(e)) from e
