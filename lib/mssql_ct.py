"""MSSQL Change Tracking reader."""

import json
import subprocess
from typing import Optional
from .journal_cache import JournalCache
from .sqlite_ct_cache import SQLiteCTCache


class MSSQLCTReader:
    """Read MSSQL Change Tracking data via qadmcli."""
    
    def __init__(self, qadmcli_path: str = "../qadmcli/qadmcli.sh", use_cache: bool = True, cache_type: str = "sqlite"):
        """
        Initialize MSSQL CT reader.
        
        Args:
            qadmcli_path: Path to qadmcli.sh
            use_cache: Whether to use CT caching (default: True)
            cache_type: Type of cache to use - "sqlite" (recommended) or "json"
        """
        self.qadmcli_path = qadmcli_path
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
    
    def _run_qadmcli(self, *args) -> dict:
        """Run qadmcli command and return parsed output."""
        cmd = [self.qadmcli_path] + list(args)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )
            try:
                # Extract JSON from output (may have shell wrapper messages and INFO logs)
                import re
                output = result.stdout
                
                # Find JSON object pattern
                match = re.search(r'\{[^{}]+\}', output, re.DOTALL)
                if match:
                    return json.loads(match.group())
                else:
                    # Fallback: try parsing entire output
                    return json.loads(output)
            except json.JSONDecodeError:
                return {'output': result.stdout, 'success': True}
        except subprocess.CalledProcessError as e:
            return {'error': e.stderr or e.stdout, 'success': False}
    
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
        should_use_cache = use_cache if use_cache is not None else self.use_cache
        if should_use_cache and self.cache and use_time_window and since:
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
            last_version = cache_info.get('last_sequence', 0)  # Reuse last_sequence field for CT version
        
        # Build command
        cmd_args = [
            "mssql", "ct", "changes",
            "-t", table_name,
            "-s", schema,
            "--format", "json"  # Get individual changes with version numbers!
        ]
        
        # If we have cached data, fetch only new changes
        # CT supports --since-version for incremental fetch
        if last_version > 0:
            cmd_args.extend(["--since-version", str(last_version + 1)])
            print(f"  ℹ️  Fetching new CT changes since version {last_version}...")
        elif since:
            cmd_args.extend(["--since", since])
            print(f"  ℹ️  Fetching CT changes since {since}...")
        else:
            print(f"  ℹ️  Fetching all CT changes (initial load)...")
        
        # Execute command
        result = self._run_qadmcli(*cmd_args)
        
        if 'error' in result:
            return {
                'table': table,
                'total': 0,
                'inserts': 0,
                'updates': 0,
                'deletes': 0,
                'from_cache': False,
                'error': result.get('error', 'Unknown error')
            }
        
        # Parse changes
        changes = result if isinstance(result, list) else result.get('changes', [])
        
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
                        last_version=changes[-1].get('sys_change_version', 0),
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
                        last_sequence=changes[-1].get('sys_change_version', 0)
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
        
        # Call qadmcli to get CT changes in JSON format
        cmd_args = [
            "mssql", "ct", "changes",
            "-t", table_name,
            "-s", schema,
            "--format", "json",
            "--limit", str(limit)
        ]
        
        if since:
            cmd_args.extend(["--since", since])
        
        result = self._run_qadmcli(*cmd_args)
        
        if 'error' in result:
            return {
                'total': 0,
                'inserts': 0,
                'updates': 0,
                'deletes': 0,
                'changes': [],
                'error': result.get('error', 'Unknown error')
            }
        
        changes = result if isinstance(result, list) else result.get('changes', [])
        
        # Categorize by operation type
        inserts = sum(1 for c in changes if c.get('SYS_CHANGE_OPERATION') == 'I' or c.get('operation') == 'I')
        updates = sum(1 for c in changes if c.get('SYS_CHANGE_OPERATION') == 'U' or c.get('operation') == 'U')
        deletes = sum(1 for c in changes if c.get('SYS_CHANGE_OPERATION') == 'D' or c.get('operation') == 'D')
        
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
        
        # Query specific record via qadmcli
        result = self._run_qadmcli(
            "mssql", "query",
            "-q", f"SELECT * FROM [{schema}].[{table_name}] WHERE [{pk_column}] = '{pk_value}'",
            "--format", "json"
        )
        
        if not result.get('success', True):
            return None
        
        rows = result.get('rows', [])
        return rows[0] if rows else None
    
    def is_ct_enabled(self, table: str) -> bool:
        """Check if Change Tracking is enabled for a table."""
        parts = table.split('.')
        if len(parts) != 2:
            return False
        
        schema, table_name = parts
        
        result = self._run_qadmcli(
            "mssql", "ct", "status",
            "-t", table_name,
            "-s", schema,
            "--format", "json"  # Add JSON format
        )
        
        if 'error' in result:
            return False
        
        return result.get('ct_enabled_on_table', False) or result.get('is_enabled_on_table', False)
