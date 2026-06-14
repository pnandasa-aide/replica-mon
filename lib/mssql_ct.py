"""MSSQL Change Tracking reader (uses direct qadmcli SDK imports)."""

import os
import json
from typing import Optional
from .journal_cache import JournalCache
from .sqlite_ct_cache import SQLiteCTCache


class MSSQLCTReader:
    """Read MSSQL Change Tracking data via direct qadmcli SDK."""
    
    def __init__(self, qadmcli_path: Optional[str] = None, use_cache: bool = True, cache_type: str = "sqlite"):
        """
        Initialize MSSQL CT reader.
        
        Args:
            qadmcli_path: Path to qadmcli.sh (unused/legacy)
            use_cache: Whether to use CT caching (default: True)
            cache_type: Type of cache to use - "sqlite" (recommended) or "json"
        """
        self.use_cache = use_cache
        
        if use_cache:
            if cache_type == "sqlite":
                # Use new SQLite-based cache (handles binary data, faster)
                self.cache = SQLiteCTCache(
                    cache_dir="cache",
                    retention_days=7
                )
            else:
                # Legacy JSON-based cache (for backward compatibility)
                self.cache = JournalCache()
        else:
            self.cache = None

    def _get_qadmcli_config(self) -> any:
        """Load connection config from standard locations or environment."""
        from qadmcli.config import load_config
        from pathlib import Path
        config_path = os.environ.get("QADMCLI_CONFIG") or "/app/qadmcli/config/connection.yaml"
        if not os.path.exists(config_path):
            config_path = "../qadmcli/config/connection.yaml"
        return load_config(Path(config_path))
    
    def get_summary(self, table: str, since: Optional[str] = None, use_time_window: bool = False) -> dict:
        """
        Get Change Tracking summary with proper caching of individual changes.
        
        Args:
            table: Table name in format "SCHEMA.TABLE"
            since: Optional timestamp in format "YYYY-MM-DD HH:MM:SS"
            use_time_window: If True, aggregate from cache by time window
            
        Returns:
            Dict with 'table', 'total', 'inserts', 'updates', 'deletes', 'from_cache'
        """
        # Always fetch new changes first (incremental update)
        # This ensures cache is up-to-date before aggregation
        fetch_result = self._fetch_from_mssql(table, since)
        
        # If time-windowed aggregation requested and cache exists, aggregate from cache
        if self.use_cache and self.cache and use_time_window and since:
            cache_info = self.cache.get_cache_info(f"CT_{table}")
            
            if cache_info['cached'] and cache_info.get('cache_level') == 'full':
                # Have full change cache - aggregate by time window
                summary = self._aggregate_ct_from_cache(table, since)
                if summary:
                    print(f"  ℹ️  Using cached CT changes for time window (fast)")
                    return summary
        
        # Return the fetch result (either no cache or aggregation failed)
        return fetch_result
    
    def _fetch_from_mssql(self, table: str, since: Optional[str] = None) -> dict:
        """
        Fetch CT changes from MSSQL and cache them.
        Performs incremental fetch if metadata exists.
        
        Args:
            table: Table name in format "SCHEMA.TABLE"
            since: Optional timestamp filter
            
        Returns:
            Summary dictionary with counts
        """
        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format SCHEMA.TABLE, got: {table}")
        
        schema, table_name = parts
        
        # Check if we have cached data to resume from
        last_version = 0
        if self.use_cache and self.cache:
            cache_info = self.cache.get_cache_info(f"CT_{table}")
            last_version = cache_info.get('last_version', 0) or cache_info.get('last_sequence', 0)
        
        from qadmcli.db.mssql import MSSQLConnection
        from qadmcli.db.mssql_ct import MSSQLChangeTracking
        from datetime import datetime

        try:
            config = self._get_qadmcli_config()
            if not config.mssql:
                raise ValueError("MSSQL configuration not found in connection.yaml")
            
            with MSSQLConnection(config.mssql) as conn:
                ct_mgr = MSSQLChangeTracking(conn)
                
                # Verify CT status first
                status = ct_mgr.get_table_ct_status(table_name, schema)
                if not status.is_enabled_on_database:
                    raise RuntimeError("Change Tracking is not enabled on the database")
                if not status.is_enabled_on_table:
                    raise RuntimeError(f"Change Tracking is not enabled on table {schema}.{table_name}")

                # Determine parameters for query
                since_timestamp = None
                since_version_param = None

                if last_version > 0:
                    since_version_param = last_version + 1
                    print(f"  ℹ️  Fetching new CT changes since version {last_version}...")
                elif since:
                    try:
                        since_timestamp = datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        try:
                            since_timestamp = datetime.strptime(since, "%Y-%m-%d")
                        except ValueError:
                            raise ValueError("Invalid timestamp format. Use YYYY-MM-DD HH:MM:SS or YYYY-MM-DD")
                    print(f"  ℹ️  Fetching CT changes since {since}...")
                else:
                    print(f"  ℹ️  Fetching all CT changes (initial load)...")

                changes_raw = ct_mgr.get_changes(
                    table_name=table_name,
                    schema=schema,
                    since_version=since_version_param,
                    since_timestamp=since_timestamp
                )

                # Format changes
                changes = []
                for c in changes_raw:
                    changes.append({
                        "SYS_CHANGE_VERSION": c.sys_change_version,
                        "SYS_CHANGE_OPERATION": c.sys_change_operation,
                        "SYS_CHANGE_COLUMNS": c.sys_change_columns,
                        "SYS_CHANGE_CONTEXT": c.sys_change_context,
                        "sys_change_version": c.sys_change_version,
                        "sys_change_operation": c.sys_change_operation,
                        "sys_change_columns": c.sys_change_columns,
                        "sys_change_context": c.sys_change_context,
                        "sys_change_timestamp": datetime.now().isoformat(),
                        "PRIMARY_KEY_VALUES": c.primary_key_values,
                        "primary_key_values": c.primary_key_values
                    })
        except Exception as e:
            return {
                'table': table,
                'total': 0,
                'inserts': 0,
                'updates': 0,
                'deletes': 0,
                'from_cache': False,
                'error': f"qadmcli SDK failed: {e}"
            }
        
        # Update cache with new changes
        if self.use_cache and self.cache and changes:
            try:
                # Check if this is SQLite cache or JSON cache
                if hasattr(self.cache, 'store_changes'):
                    # SQLite cache - use store_changes method
                    cache_table_name = f"CT_{table}"
                    new_count = self.cache.store_changes(
                        cache_table_name,
                        changes,
                        last_version=changes[-1].get('SYS_CHANGE_VERSION', 0),
                        last_timestamp=changes[-1].get('sys_change_timestamp')
                    )
                    print(f"  ✓ Cached {new_count} new CT changes (total: {len(changes)})")
                else:
                    # JSON cache - use append_entries method (legacy)
                    cache_table_name = f"CT_{table}"
                    new_count = self.cache.append_entries(
                        cache_table_name,
                        changes,
                        last_timestamp=changes[-1].get('sys_change_timestamp'),
                        last_sequence=changes[-1].get('SYS_CHANGE_VERSION', 0)
                    )
                    
                    # Update metadata to mark as full cache
                    meta_path = self.cache._get_metadata_path(cache_table_name)
                    if meta_path.exists():
                        import json
                        with open(meta_path, 'r') as f:
                            metadata = json.load(f)
                        metadata['cache_level'] = 'full'
                        with open(meta_path, 'w') as f:
                            json.dump(metadata, f, indent=2)
                    
                    print(f"  ✓ Cached {new_count} new CT changes (total: {len(changes)})")
            except Exception as e:
                print(f"  ⚠️  Warning: Could not update CT cache: {e}")
        
        # Aggregate counts
        summary = self._count_ct_by_operation(changes)
        summary['table'] = table
        summary['from_cache'] = False
        
        return summary
    
    def _aggregate_ct_from_cache(self, table: str, since: str) -> dict:
        """
        Aggregate CT changes from cache by time window.
        
        Args:
            table: Table name
            since: Start of time window "YYYY-MM-DD HH:MM:SS"
            
        Returns:
            Summary dict or None if cache unavailable
        """
        if not self.cache:
            return None
        
        try:
            # Load cached changes (use CT_ prefix)
            cache_table_name = f"CT_{table}"
            
            # Check if this is SQLite cache or JSON cache
            if hasattr(self.cache, 'get_changes'):
                # SQLite cache - use fast indexed query
                changes = self.cache.get_changes(cache_table_name, since=since)
            else:
                # JSON cache - load and filter (legacy)
                cache_data = self.cache.load_cache(cache_table_name)
                all_changes = cache_data.get('entries', [])
                changes = [c for c in all_changes if c.get('sys_change_timestamp', '') >= since]
            
            if not changes:
                return {
                    'table': table,
                    'total': 0,
                    'inserts': 0,
                    'updates': 0,
                    'deletes': 0,
                    'from_cache': True
                }
            
            # Aggregate by operation
            summary = self._count_ct_by_operation(changes)
            summary['table'] = table
            summary['from_cache'] = True
            
            return summary
        except Exception as e:
            print(f"  ⚠️  CT cache aggregation error: {e}")
            return None
    
    def _count_ct_by_operation(self, changes: list) -> dict:
        """
        Count CT changes by operation type.
        
        Args:
            changes: List of CT change dicts
            
        Returns:
            Dict with total, inserts, updates, deletes
        """
        inserts = 0
        updates = 0
        deletes = 0
        
        for change in changes:
            operation = change.get('sys_change_operation', '')
            
            # MSSQL CT operation codes:
            # I = Insert
            # U = Update
            # D = Delete
            if operation == 'I':
                inserts += 1
            elif operation == 'U':
                updates += 1
            elif operation == 'D':
                deletes += 1
        
        return {
            'total': len(changes),
            'inserts': inserts,
            'updates': updates,
            'deletes': deletes
        }
    
    def get_changes(self, table: str, since: Optional[str] = None, limit: int = 1000) -> dict:
        """
        Get Change Tracking changes for a table.
        
        Args:
            table: Table name in format "SCHEMA.TABLE"
            since: Optional timestamp in format "YYYY-MM-DD HH:MM:SS"
            limit: Maximum changes to return
            
        Returns:
            Dict with 'total', 'inserts', 'updates', 'deletes', 'changes'
        """
        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format SCHEMA.TABLE, got: {table}")
        
        schema, table_name = parts
        
        from qadmcli.db.mssql import MSSQLConnection
        from qadmcli.db.mssql_ct import MSSQLChangeTracking
        from datetime import datetime

        try:
            config = self._get_qadmcli_config()
            if not config.mssql:
                raise ValueError("MSSQL configuration not found in connection.yaml")
            
            # Parse timestamp if provided
            since_timestamp = None
            if since:
                try:
                    since_timestamp = datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    try:
                        since_timestamp = datetime.strptime(since, "%Y-%m-%d")
                    except ValueError:
                        raise ValueError("Invalid timestamp format. Use YYYY-MM-DD HH:MM:SS or YYYY-MM-DD")

            with MSSQLConnection(config.mssql) as conn:
                ct_mgr = MSSQLChangeTracking(conn)
                changes_raw = ct_mgr.get_changes(
                    table_name=table_name,
                    schema=schema,
                    since_timestamp=since_timestamp
                )
                
                # Format changes
                changes = []
                for c in changes_raw:
                    changes.append({
                        "SYS_CHANGE_VERSION": c.sys_change_version,
                        "SYS_CHANGE_OPERATION": c.sys_change_operation,
                        "SYS_CHANGE_COLUMNS": c.sys_change_columns,
                        "SYS_CHANGE_CONTEXT": c.sys_change_context,
                        "PRIMARY_KEY_VALUES": c.primary_key_values,
                        "PRIMARY_KEY": c.primary_key_values,
                        "version": c.sys_change_version,
                        "operation": c.sys_change_operation,
                        "pk": c.primary_key_values
                    })
        except Exception as e:
            return {
                'total': 0,
                'inserts': 0,
                'updates': 0,
                'deletes': 0,
                'changes': [],
                'error': f"qadmcli SDK failed: {e}"
            }
        
        # Categorize by operation type
        inserts = sum(1 for c in changes if c.get('SYS_CHANGE_OPERATION') == 'I')
        updates = sum(1 for c in changes if c.get('SYS_CHANGE_OPERATION') == 'U')
        deletes = sum(1 for c in changes if c.get('SYS_CHANGE_OPERATION') == 'D')
        
        return {
            'total': len(changes),
            'inserts': inserts,
            'updates': updates,
            'deletes': deletes,
            'changes': changes[:limit]
        }
    
    def get_record(self, table: str, pk_column: str, pk_value: str) -> Optional[dict]:
        """
        Get a specific record by primary key.
        
        Args:
            table: Table name in format "SCHEMA.TABLE"
            pk_column: Primary key column name
            pk_value: Primary key value
            
        Returns:
            Record dict or None if not found
        """
        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format SCHEMA.TABLE, got: {table}")
        
        schema, table_name = parts
        
        from qadmcli.db.mssql import MSSQLConnection

        try:
            config = self._get_qadmcli_config()
            if not config.mssql:
                raise ValueError("MSSQL configuration not found in connection.yaml")
            
            with MSSQLConnection(config.mssql) as conn:
                # Query specific record directly via native SQL execute
                sql = f"SELECT * FROM [{schema}].[{table_name}] WHERE [{pk_column}] = ?"
                with conn.get_cursor() as cursor:
                    cursor.execute(sql, (pk_value,))
                    desc = cursor.description
                    row = cursor.fetchone()
                    
                    if not row or not desc:
                        return None
                    
                    return {desc[i][0]: row[i] for i in range(len(desc))}
        except Exception as e:
            print(f"⚠️ get_record SDK failed: {e}")
            return None
    
    def is_ct_enabled(self, table: str) -> bool:
        """Check if Change Tracking is enabled for a table."""
        parts = table.split('.')
        if len(parts) != 2:
            return False
        
        schema, table_name = parts
        
        from qadmcli.db.mssql import MSSQLConnection
        from qadmcli.db.mssql_ct import MSSQLChangeTracking

        try:
            config = self._get_qadmcli_config()
            if not config.mssql:
                return False
            
            with MSSQLConnection(config.mssql) as conn:
                ct_mgr = MSSQLChangeTracking(conn)
                status = ct_mgr.get_table_ct_status(table_name, schema)
                return bool(status.is_enabled_on_table)
        except Exception:
            return False
