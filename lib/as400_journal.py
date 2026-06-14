"""AS400 journal reader (uses direct qadmcli SDK imports)."""

import os
import json
from typing import Optional
from .journal_cache import JournalCache
from .sqlite_journal_cache import SQLiteJournalCache


class AS400JournalReader:
    """Read AS400 journal entries via qadmcli with caching support."""
    
    def __init__(self, use_cache: bool = True, cache_type: str = "sqlite"):
        """
        Initialize AS400 journal reader.
        
        Args:
            use_cache: Whether to use journal caching (default: True)
            cache_type: Type of cache to use - "sqlite" (recommended) or "json"
        """
        self.use_cache = use_cache
        
        if use_cache:
            if cache_type == "sqlite":
                # Use new SQLite-based cache (handles binary data, faster)
                self.cache = SQLiteJournalCache(
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
        Get journal summary with proper caching of individual entries.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            since: Optional timestamp in format "YYYY-MM-DD HH:MM:SS"
            use_time_window: If True, aggregate from cache by time window
            
        Returns:
            Dict with 'table', 'total', 'inserts', 'updates', 'deletes', 'from_cache'
        """
        # Always fetch new entries first (incremental update)
        # This ensures cache is up-to-date before aggregation
        fetch_result = self._fetch_from_as400(table, since)
        
        # If time-windowed aggregation requested and cache exists, aggregate from cache
        if self.use_cache and self.cache and use_time_window and since:
            cache_info = self.cache.get_cache_info(table)
            
            if cache_info['cached'] and cache_info.get('cache_level') == 'full':
                # Have full entry cache - aggregate by time window
                summary = self._aggregate_from_cache(table, since)
                if summary:
                    print(f"  ℹ️  Using cached entries for time window (fast)")
                    return summary
        
        # Return the fetch result (either no cache or aggregation failed)
        return fetch_result
    
    def _fetch_from_as400(self, table: str, since: Optional[str] = None) -> dict:
        """
        Fetch journal entries from AS400 and cache them.
        Performs incremental fetch if metadata exists.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            since: Optional timestamp filter
            
        Returns:
            Summary dictionary with counts
        """
        from qadmcli.db.connection import AS400ConnectionManager
        from qadmcli.db.journal import JournalManager

        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format LIBRARY.TABLE, got: {table}")
        
        library, table_name = parts
        
        # Check if we have cached data to resume from
        last_sequence = 0
        last_timestamp = None
        if self.use_cache and self.cache:
            cache_info = self.cache.get_cache_info(table)
            last_sequence = cache_info.get('last_sequence', 0)
            last_timestamp = cache_info.get('last_timestamp')
        
        # Determine from-time parameter
        from_time = None
        if last_timestamp:
            from_time = last_timestamp
            print(f"  ℹ️  Fetching new journal entries since {last_timestamp}...")
        elif since:
            from_time = since
            print(f"  ℹ️  Fetching journal entries since {since}...")
        else:
            print(f"  ℹ️  Fetching all journal entries (initial load)...")
        
        try:
            config = self._get_qadmcli_config()
            with AS400ConnectionManager(config) as conn:
                jrn_mgr = JournalManager(conn)
                # Fetch entries directly from database using SDK library logic
                entries_raw = jrn_mgr.get_journal_entries(
                    table_name=table_name,
                    library=library,
                    from_time=from_time,
                    limit=100000
                )
                entries = [e.model_dump() for e in entries_raw]
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
        
        # Update cache with new entries
        if self.use_cache and self.cache and entries:
            try:
                # Check if this is SQLite cache or JSON cache
                if hasattr(self.cache, 'store_entries'):
                    # SQLite cache - use store_entries method
                    new_count = self.cache.store_entries(
                        table,
                        entries,
                        last_sequence=entries[-1].get('entry_number', 0),
                        last_timestamp=entries[-1].get('entry_timestamp')
                    )
                else:
                    # JSON cache - use append_entries method (legacy)
                    new_count = self.cache.append_entries(
                        table,
                        entries,
                        last_timestamp=entries[-1].get('entry_timestamp'),
                        last_sequence=entries[-1].get('entry_number', 0)
                    )
                    
                    # Update metadata to mark as full cache
                    meta_path = self.cache._get_metadata_path(table)
                    if meta_path.exists():
                        import json
                        with open(meta_path, 'r') as f:
                            metadata = json.load(f)
                        metadata['cache_level'] = 'full'
                        with open(meta_path, 'w') as f:
                            json.dump(metadata, f, indent=2)
                
                print(f"  ✓ Cached {new_count} new entries (total: {len(entries)})")
            except Exception as e:
                print(f"  ⚠️  Warning: Could not update cache: {e}")
        
        # Aggregate counts
        summary = self._count_by_type(entries)
        summary['table'] = table
        summary['from_cache'] = False
        
        return summary
    
    def _aggregate_from_cache(self, table: str, since: str) -> Optional[dict]:
        """
        Aggregate journal entries from cache by time window.
        
        Args:
            table: Table name
            since: Start of time window "YYYY-MM-DD HH:MM:SS"
            
        Returns:
            Summary dict or None if cache unavailable
        """
        if not self.cache:
            return None
        
        try:
            # Check if this is SQLite cache or JSON cache
            if hasattr(self.cache, 'get_entries'):
                # SQLite cache - use fast indexed query
                entries = self.cache.get_entries(table, since=since)
            else:
                # JSON cache - load and filter (legacy)
                cache_data = self.cache.load_cache(table)
                all_entries = cache_data.get('entries', [])
                entries = [e for e in all_entries if e.get('entry_timestamp', '') >= since]
            
            if not entries:
                return {
                    'table': table,
                    'total': 0,
                    'inserts': 0,
                    'updates': 0,
                    'deletes': 0,
                    'from_cache': True
                }
            
            # Aggregate by type
            summary = self._count_by_type(entries)
            summary['table'] = table
            summary['from_cache'] = True
            
            return summary
        except Exception as e:
            print(f"  ⚠️  Cache aggregation error: {e}")
            return None
    
    def _count_by_type(self, entries: list) -> dict:
        """
        Count journal entries by operation type.
        
        Args:
            entries: List of journal entry dicts
            
        Returns:
            Dict with total, inserts, updates, deletes
        """
        inserts = 0
        updates = 0
        deletes = 0
        
        for entry in entries:
            entry_type = entry.get('entry_type', '')
            
            # AS400 journal entry types:
            # PT = Add/Insert
            # UP = Update before image
            # UB = Update after image  
            # DL = Delete
            if entry_type == 'PT':
                inserts += 1
            elif entry_type in ('UP', 'UB'):
                updates += 1
            elif entry_type == 'DL':
                deletes += 1
        
        return {
            'total': len(entries),
            'inserts': inserts,
            'updates': updates,
            'deletes': deletes
        }
    
    def get_changes(self, table: str, since: Optional[str] = None, limit: int = 100) -> dict:
        """
        Get journal entries for a table.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            since: Optional timestamp in format "YYYY-MM-DD HH:MM:SS"
            limit: Maximum entries to return
            
        Returns:
            Dict with 'total', 'inserts', 'updates', 'deletes', 'entries'
        """
        from qadmcli.db.connection import AS400ConnectionManager
        from qadmcli.db.journal import JournalManager

        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format LIBRARY.TABLE, got: {table}")
        
        library, table_name = parts
        
        try:
            config = self._get_qadmcli_config()
            with AS400ConnectionManager(config) as conn:
                jrn_mgr = JournalManager(conn)
                entries_raw = jrn_mgr.get_journal_entries(
                    table_name=table_name,
                    library=library,
                    from_time=since,
                    limit=limit
                )
                entries = [e.model_dump() for e in entries_raw]
        except Exception as e:
            raise RuntimeError(f"qadmcli SDK failed: {e}")
        
        inserts = sum(1 for e in entries if e.get('entry_type') == 'PT')
        updates = sum(1 for e in entries if e.get('entry_type') in ('UP', 'UB'))
        deletes = sum(1 for e in entries if e.get('entry_type') == 'DL')
        
        # Standardize entry key format (e.g. entry_type maps to code)
        formatted_entries = []
        for e in entries:
            fe = dict(e)
            fe.setdefault('code', e.get('entry_type', ''))
            formatted_entries.append(fe)
        
        return {
            'total': len(entries),
            'inserts': inserts,
            'updates': updates,
            'deletes': deletes,
            'entries': formatted_entries[:limit]
        }
    
    def get_record(self, table: str, pk_column: str, pk_value: str) -> Optional[dict]:
        """
        Get a specific record by primary key.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            pk_column: Primary key column name
            pk_value: Primary key value
            
        Returns:
            Record dict or None if not found
        """
        from qadmcli.db.connection import AS400ConnectionManager

        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format LIBRARY.TABLE, got: {table}")
        
        library, table_name = parts
        
        try:
            config = self._get_qadmcli_config()
            with AS400ConnectionManager(config) as conn:
                # Query specific record directly via native SQL execute
                sql = f"SELECT * FROM {library}.{table_name} WHERE {pk_column} = ?"
                cursor = conn.execute(sql, (pk_value,))
                
                # Fetch row descriptions to build dictionary
                desc = cursor.description
                row = cursor.fetchone()
                cursor.close()
                
                if not row or not desc:
                    return None
                
                return {desc[i][0]: row[i] for i in range(len(desc))}
        except Exception as e:
            print(f"⚠️ get_record SDK failed: {e}")
            return None
