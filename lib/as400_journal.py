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
                
                # Find JSON object - handle nested structures
                # Look for first { and match to last }
                start_idx = clean_output.find('{')
                if start_idx >= 0:
                    # Find the matching closing brace
                    brace_count = 0
                    end_idx = start_idx
                    for i in range(start_idx, len(clean_output)):
                        if clean_output[i] == '{':
                            brace_count += 1
                        elif clean_output[i] == '}':
                            brace_count -= 1
                            if brace_count == 0:
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
    
    def get_summary(self, table: str, since: Optional[str] = None) -> dict:
        """
        Get journal summary for comparison with MSSQL CT (with caching).
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            since: Optional timestamp in format "YYYY-MM-DD HH:MM:SS"
            
        Returns:
            Dict with 'table', 'total', 'inserts', 'updates', 'deletes', 'entries'
        """
        # Try cache first
        if self.use_cache and self.cache:
            cache_info = self.cache.get_cache_info(table)
            
            if cache_info['cached']:
                # Check if cache has summary data
                if cache_info['entry_count'] == 0 and cache_info.get('cache_level') == 'summary':
                    # Summary cache with no individual entries - this is expected
                    # Check if we have summary metadata
                    meta_path = self.cache._get_metadata_path(table)
                    if meta_path.exists():
                        try:
                            import json
                            with open(meta_path, 'r') as f:
                                meta = json.load(f)
                            
                            # Check if summary data is recent (< 1 hour old)
                            if 'summary_cached_at' in meta:
                                from datetime import datetime
                                cached_time = datetime.strptime(meta['summary_cached_at'], "%Y-%m-%d %H:%M:%S")
                                age_minutes = (datetime.now() - cached_time).total_seconds() / 60
                                
                                if age_minutes < 60:  # Cache valid for 1 hour
                                    print(f"  ℹ️  Using summary cache ({age_minutes:.0f}m old, {meta.get('summary_total', 0)} entries)")
                                    return {
                                        'table': table,
                                        'total': meta.get('summary_total', 0),
                                        'inserts': meta.get('summary_inserts', 0),
                                        'updates': meta.get('summary_updates', 0),
                                        'deletes': meta.get('summary_deletes', 0),
                                        'from_cache': True
                                    }
                        except Exception as e:
                            print(f"  ⚠️  Cache read error: {e}")
                    
                    # No valid summary metadata - re-fetch
                    from datetime import datetime
                    cached_time = datetime.strptime(cache_info['cached_at'], "%Y-%m-%d %H:%M:%S")
                    age_hours = (datetime.now() - cached_time).total_seconds() / 3600
                    print(f"  ℹ️  Summary cache expired ({age_hours:.1f}h ago), re-fetching from AS400...")
                # Have valid cache - check if we need to update it
                elif since and cache_info['last_timestamp']:
                    # User wants data since specific time
                    if since <= cache_info['last_timestamp']:
                        # Requested time is within cache range - use cache!
                        summary = self.cache.get_summary_from_cache(table, since)
                        print(f"  ℹ️  Using cached data ({cache_info['entry_count']} entries)")
                        return summary
                    else:
                        # Need newer data - fetch from AS400
                        print(f"  ℹ️  Cache outdated, fetching from AS400...")
                elif not since:
                    # No time filter - use full cache
                    summary = self.cache.get_summary_from_cache(table)
                    print(f"  ℹ️  Using cached data ({cache_info['entry_count']} entries)")
                    return summary
        
        # Fetch from AS400
        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format LIBRARY.TABLE, got: {table}")
        
        library, table_name = parts
        
        # Call qadmcli with summary format
        cmd_args = [
            "journal", "entries",
            "-t", table_name,
            "-l", library,
            "--format", "summary"
        ]
        
        if since:
            cmd_args.extend(["--from-time", since])
            print(f"  ℹ️  Fetching journal entries since {since}...")
        else:
            print(f"  ℹ️  Fetching all journal entries (this may take a while)...")
        
        result = self._run_qadmcli(*cmd_args)
        
        if 'error' in result:
            return {
                'table': table,
                'total': 0,
                'inserts': 0,
                'updates': 0,
                'deletes': 0,
                'entries': [],
                'error': result.get('error', 'Unknown error')
            }
        
        # Update cache with new data
        if self.use_cache and self.cache and not since:
            # Only cache full summary (not filtered)
            try:
                # Save summary counts to cache metadata
                # We don't store individual entries in summary mode
                self.cache.save_cache(
                    table,
                    entries=[],  # Summary mode: no individual entries
                    last_timestamp=result.get('newest_timestamp'),
                    last_sequence=result.get('newest_sequence', 0),
                    cache_level="summary",
                    metadata={
                        'summary_inserts': result.get('inserts', 0),
                        'summary_updates': result.get('updates', 0),
                        'summary_deletes': result.get('deletes', 0),
                        'summary_total': result.get('total', 0),
                        'summary_cached_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                )
                print(f"  ✓ Summary cache updated: {result.get('total', 0)} total entries")
            except Exception as e:
                print(f"  ⚠️  Warning: Could not update cache: {e}")
        
        result['from_cache'] = False
        return result
    
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
