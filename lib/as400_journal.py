"""AS400 journal reader (wraps qadmcli container)."""

import json
import subprocess
import re
from typing import Optional
from .journal_cache import JournalCache


class AS400JournalReader:
    """Read AS400 journal entries via qadmcli with caching support."""
    
    def __init__(self, qadmcli_path: str = "../qadmcli/qadmcli.sh", use_cache: bool = True):
        """
        Initialize AS400 journal reader.
        
        Args:
            qadmcli_path: Path to qadmcli.sh
            use_cache: Whether to use journal caching (default: True)
        """
        self.qadmcli_path = qadmcli_path
        self.use_cache = use_cache
        self.cache = JournalCache() if use_cache else None
    
    def _run_qadmcli(self, *args) -> dict:
        """Run qadmcli command and return parsed output."""
        cmd = [self.qadmcli_path] + list(args)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=120  # 2 minute timeout
            )
            # Try to parse as JSON if possible
            try:
                # Extract JSON from output (may have wrapper messages and trailing logs)
                output = result.stdout
                
                # Strip ANSI escape codes from entire output first
                ansi_escape = re.compile(r'\x1b\[[0-9;]*m')
                clean_output = ansi_escape.sub('', output)
                
                # Find JSON - handle both objects {} and arrays []
                # Look for first { or [
                start_idx_obj = clean_output.find('{')
                start_idx_arr = clean_output.find('[')
                
                if start_idx_obj >= 0 or start_idx_arr >= 0:
                    # Use whichever comes first
                    if start_idx_obj >= 0 and (start_idx_arr < 0 or start_idx_obj < start_idx_arr):
                        start_idx = start_idx_obj
                        open_char = '{'
                        close_char = '}'
                    else:
                        start_idx = start_idx_arr
                        open_char = '['
                        close_char = ']'
                    
                    # Find the matching closing bracket/brace
                    bracket_count = 0
                    end_idx = start_idx
                    for i in range(start_idx, len(clean_output)):
                        if clean_output[i] == open_char:
                            bracket_count += 1
                        elif clean_output[i] == close_char:
                            bracket_count -= 1
                            if bracket_count == 0:
                                end_idx = i + 1
                                break
                    
                    json_str = clean_output[start_idx:end_idx]
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError as e:
                        # JSON extraction failed, log and fallback
                        print(f"  ⚠️  JSON parse error: {e}")
                        print(f"  ℹ️  Extracted (first 200 chars): {json_str[:200]}")
                
                # Fallback: try parsing entire output
                return json.loads(clean_output)
            except json.JSONDecodeError:
                # Complete failure - return raw output for debugging
                return {'output': result.stdout, 'success': True}
        except subprocess.TimeoutExpired:
            return {'error': 'Command timed out after 120 seconds', 'success': False}
        except subprocess.CalledProcessError as e:
            return {'error': e.stderr or e.stdout, 'success': False}
    
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
        # Try cache first for time-windowed queries
        if self.use_cache and self.cache and use_time_window and since:
            # Check if we can serve from cache
            cache_info = self.cache.get_cache_info(table)
            
            if cache_info['cached'] and cache_info.get('cache_level') == 'full':
                # Have full entry cache - aggregate by time window
                summary = self._aggregate_from_cache(table, since)
                if summary:
                    print(f"  ℹ️  Using cached entries for time window (fast)")
                    return summary
        
        # Fetch from AS400 (initial load or incremental update)
        return self._fetch_from_as400(table, since)
    
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
        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format LIBRARY.TABLE, got: {table}")
        
        library, table_name = parts
        
        # Check if we have cached data to resume from
        last_sequence = 0
        if self.use_cache and self.cache:
            cache_info = self.cache.get_cache_info(table)
            last_sequence = cache_info.get('last_sequence', 0)
        
        # Build command
        cmd_args = [
            "journal", "entries",
            "-t", table_name,
            "-l", library,
            "--format", "json"  # Get individual entries with timestamps!
        ]
        
        # If we have cached data, fetch only new entries
        if last_sequence > 0:
            cmd_args.extend(["--from-sequence", str(last_sequence + 1)])
            print(f"  ℹ️  Fetching new journal entries since sequence {last_sequence}...")
        elif since:
            cmd_args.extend(["--from-time", since])
            print(f"  ℹ️  Fetching journal entries since {since}...")
        else:
            print(f"  ℹ️  Fetching all journal entries (initial load, this may take a while)...")
        
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
        
        # Parse entries
        entries = result if isinstance(result, list) else result.get('entries', [])
        
        # Update cache with new entries
        if self.use_cache and self.cache and entries:
            try:
                # Append new entries to cache
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
            # Load cached entries
            cache_data = self.cache.load_cache(table)
            entries = cache_data.get('entries', [])
            
            if not entries:
                return None
            
            # Filter entries by time window
            window_entries = [
                e for e in entries
                if e.get('entry_timestamp', '') >= since
            ]
            
            if not window_entries:
                # No entries in this window
                return {
                    'table': table,
                    'total': 0,
                    'inserts': 0,
                    'updates': 0,
                    'deletes': 0,
                    'from_cache': True
                }
            
            # Aggregate by type
            summary = self._count_by_type(window_entries)
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
        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format LIBRARY.TABLE, got: {table}")
        
        library, table_name = parts
        
        # Call qadmcli to get journal entries
        cmd_args = [
            "journal", "entries",
            "-t", table_name,
            "-l", library,
            "--format", "json",
            "--limit", str(limit)
        ]
        
        if since:
            cmd_args.extend(["--from-time", since])
        
        result = self._run_qadmcli(*cmd_args)
        
        if 'error' in result:
            raise RuntimeError(f"qadmcli failed: {result.get('error')}")
        
        # Parse and categorize entries
        entries = result if isinstance(result, list) else result.get('entries', [])
        
        inserts = sum(1 for e in entries if e.get('code') == 'PT')
        updates = sum(1 for e in entries if e.get('code') == 'UP')
        deletes = sum(1 for e in entries if e.get('code') == 'DL')
        
        return {
            'total': len(entries),
            'inserts': inserts,
            'updates': updates,
            'deletes': deletes,
            'entries': entries[:limit]
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
        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format LIBRARY.TABLE, got: {table}")
        
        library, table_name = parts
        
        # Query specific record via qadmcli
        result = self._run_qadmcli(
            "sql", "execute",
            "-q", f"SELECT * FROM {library}.{table_name} WHERE {pk_column} = '{pk_value}'",
            "--format", "json"
        )
        
        if not result.get('success', True):
            return None
        
        rows = result.get('rows', [])
        return rows[0] if rows else None
