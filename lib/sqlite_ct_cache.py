#!/usr/bin/env python3
"""
SQLite-based cache for MSSQL Change Tracking entries.

Mirrors the SQLiteJournalCache architecture for consistency.
"""

import sqlite3
import os
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import contextmanager


class SQLiteCTCache:
    """
    SQLite-based cache for MSSQL Change Tracking entries.
    
    Features:
    - Stores CT changes with version tracking
    - Fast indexed queries by version and timestamp
    - Automatic cleanup of old entries
    - Incremental updates using --since-version
    """
    
    def __init__(self, cache_dir: str, retention_days: int = 7):
        """
        Initialize SQLite CT cache.
        
        Args:
            cache_dir: Directory to store cache database
            retention_days: Number of days to keep entries (default: 7)
        """
        os.makedirs(cache_dir, exist_ok=True)
        self.db_path = os.path.join(cache_dir, "ct_cache.db")
        self.retention_days = retention_days
        
        # Initialize database
        self._init_db()
        
        # Auto-cleanup on startup
        self._cleanup_old_entries()
    
    @contextmanager
    def _get_connection(self):
        """Get database connection with context manager."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def _init_db(self):
        """Initialize database schema with indexes."""
        with self._get_connection() as conn:
            # CT changes table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ct_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    table_name TEXT NOT NULL,
                    sys_change_version INTEGER NOT NULL,
                    sys_change_operation TEXT NOT NULL,
                    sys_change_columns TEXT,
                    sys_change_context TEXT,
                    sys_change_timestamp TEXT,
                    primary_key_values BLOB,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(table_name, sys_change_version)
                )
            """)
            
            # Metadata table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache_metadata (
                    table_name TEXT PRIMARY KEY,
                    last_version INTEGER,
                    last_timestamp TEXT,
                    entry_count INTEGER,
                    cache_level TEXT DEFAULT 'full',
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Indexes for fast queries
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ct_table_version 
                ON ct_changes(table_name, sys_change_version)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ct_table_timestamp 
                ON ct_changes(table_name, sys_change_timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ct_timestamp 
                ON ct_changes(sys_change_timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_ct_operation 
                ON ct_changes(table_name, sys_change_operation)
            """)
    
    def store_changes(self, table_name: str, changes: List[Dict[str, Any]],
                     last_version: Optional[int] = None,
                     last_timestamp: Optional[str] = None) -> int:
        """
        Store CT changes in SQLite cache.
        
        Args:
            table_name: Table name (e.g., "dbo.CUSTOMERS")
            changes: List of CT change dicts
            last_version: Last change version number
            last_timestamp: Last change timestamp
            
        Returns:
            Number of changes stored
        """
        if not changes:
            return 0
        
        with self._get_connection() as conn:
            stored_count = 0
            
            for change in changes:
                # Convert primary_key_values to BLOB
                pk_values = self._to_blob(change.get('PRIMARY_KEY_VALUES'))
                
                # Insert or replace
                conn.execute("""
                    INSERT OR REPLACE INTO ct_changes 
                    (table_name, sys_change_version, sys_change_operation,
                     sys_change_columns, sys_change_context, sys_change_timestamp,
                     primary_key_values)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    table_name,
                    change.get('SYS_CHANGE_VERSION'),
                    change.get('SYS_CHANGE_OPERATION'),
                    change.get('SYS_CHANGE_COLUMNS'),
                    change.get('SYS_CHANGE_CONTEXT'),
                    change.get('SYS_CHANGE_TIMESTAMP'),
                    pk_values
                ))
                stored_count += 1
            
            # Update metadata
            if last_version is not None and last_timestamp is not None:
                conn.execute("""
                    INSERT OR REPLACE INTO cache_metadata 
                    (table_name, last_version, last_timestamp, entry_count, cache_level, updated_at)
                    VALUES (?, ?, ?, ?, 'full', ?)
                """, (
                    table_name,
                    last_version,
                    last_timestamp,
                    stored_count,
                    datetime.now().isoformat()
                ))
            
            return stored_count
    
    def get_changes(self, table_name: str, since_version: Optional[int] = None,
                   since: Optional[str] = None, until: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get CT changes with optional filters.
        
        Args:
            table_name: Table name to query
            since_version: Start version (inclusive)
            since: Start timestamp
            until: End timestamp
            
        Returns:
            List of CT change dicts
        """
        with self._get_connection() as conn:
            query = "SELECT * FROM ct_changes WHERE table_name = ?"
            params = [table_name]
            
            if since_version is not None:
                query += " AND sys_change_version >= ?"
                params.append(since_version)
            
            if since:
                query += " AND sys_change_timestamp >= ?"
                params.append(since)
            
            if until:
                query += " AND sys_change_timestamp <= ?"
                params.append(until)
            
            query += " ORDER BY sys_change_version"
            
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            
            # Convert to dicts
            changes = []
            for row in rows:
                change = dict(row)
                change['PRIMARY_KEY_VALUES'] = self._from_blob(change.get('primary_key_values'))
                changes.append(change)
            
            return changes
    
    def get_change_count(self, table_name: str, since_version: Optional[int] = None,
                        since: Optional[str] = None, until: Optional[str] = None) -> int:
        """
        Get count of CT changes with optional filters.
        
        Args:
            table_name: Table name
            since_version: Start version
            since: Start timestamp
            until: End timestamp
            
        Returns:
            Number of changes
        """
        with self._get_connection() as conn:
            query = "SELECT COUNT(*) as count FROM ct_changes WHERE table_name = ?"
            params = [table_name]
            
            if since_version is not None:
                query += " AND sys_change_version >= ?"
                params.append(since_version)
            
            if since:
                query += " AND sys_change_timestamp >= ?"
                params.append(since)
            
            if until:
                query += " AND sys_change_timestamp <= ?"
                params.append(until)
            
            cursor = conn.execute(query, params)
            return cursor.fetchone()['count']
    
    def get_changes_by_operation(self, table_name: str, operation: str,
                                since: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get changes filtered by operation type (I/U/D).
        
        Args:
            table_name: Table name
            operation: Operation type ('I', 'U', or 'D')
            since: Optional start timestamp
            
        Returns:
            List of matching changes
        """
        with self._get_connection() as conn:
            query = """
                SELECT * FROM ct_changes 
                WHERE table_name = ? AND sys_change_operation = ?
            """
            params = [table_name, operation]
            
            if since:
                query += " AND sys_change_timestamp >= ?"
                params.append(since)
            
            query += " ORDER BY sys_change_version"
            
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            
            changes = []
            for row in rows:
                change = dict(row)
                change['PRIMARY_KEY_VALUES'] = self._from_blob(change.get('primary_key_values'))
                changes.append(change)
            
            return changes
    
    def get_cache_info(self, table_name: str) -> Dict[str, Any]:
        """
        Get cache metadata for a table.
        
        Args:
            table_name: Table name
            
        Returns:
            Cache metadata dict
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM cache_metadata WHERE table_name = ?",
                (table_name,)
            )
            row = cursor.fetchone()
            
            if row:
                return dict(row)
            else:
                return {
                    'table_name': table_name,
                    'last_version': 0,
                    'last_timestamp': None,
                    'entry_count': 0,
                    'cache_level': 'none',
                    'cached': False
                }
    
    def clear_cache(self, table_name: Optional[str] = None):
        """
        Clear cache for a specific table or all tables.
        
        Args:
            table_name: Table name to clear, or None for all
        """
        with self._get_connection() as conn:
            if table_name:
                conn.execute("DELETE FROM ct_changes WHERE table_name = ?", (table_name,))
                conn.execute("DELETE FROM cache_metadata WHERE table_name = ?", (table_name,))
            else:
                conn.execute("DELETE FROM ct_changes")
                conn.execute("DELETE FROM cache_metadata")
    
    def _cleanup_old_entries(self):
        """Remove entries older than retention period."""
        cutoff_date = (datetime.now() - timedelta(days=self.retention_days)).isoformat()
        
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM ct_changes WHERE sys_change_timestamp < ?",
                (cutoff_date,)
            )
            deleted = cursor.rowcount
            
            if deleted > 0:
                conn.execute("VACUUM")
    
    def _to_blob(self, data: Any) -> Optional[bytes]:
        """Convert data to BLOB for storage."""
        if data is None:
            return None
        elif isinstance(data, bytes):
            return data
        elif isinstance(data, dict):
            import json
            return json.dumps(data).encode('utf-8')
        elif isinstance(data, str):
            return data.encode('utf-8')
        else:
            return str(data).encode('utf-8')
    
    def _from_blob(self, blob_data: Optional[bytes]) -> Any:
        """Convert BLOB back to original data type."""
        if blob_data is None:
            return None
        
        try:
            text = blob_data.decode('utf-8')
            import json
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        except UnicodeDecodeError:
            return blob_data.hex()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT COUNT(*) as count FROM ct_changes")
            total_entries = cursor.fetchone()['count']
            
            cursor = conn.execute("""
                SELECT table_name, COUNT(*) as count 
                FROM ct_changes 
                GROUP BY table_name
            """)
            by_table = {row['table_name']: row['count'] for row in cursor.fetchall()}
            
            db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
            
            cursor = conn.execute("""
                SELECT MIN(sys_change_timestamp) as oldest, MAX(sys_change_timestamp) as newest
                FROM ct_changes
            """)
            row = cursor.fetchone()
            oldest = row['oldest']
            newest = row['newest']
            
            return {
                'total_entries': total_entries,
                'tables': by_table,
                'db_size_bytes': db_size,
                'db_size_mb': round(db_size / 1024 / 1024, 2),
                'oldest_entry': oldest,
                'newest_entry': newest,
                'retention_days': self.retention_days,
                'db_path': self.db_path
            }
