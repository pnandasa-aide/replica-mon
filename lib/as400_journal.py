"""AS400 journal reader (wraps qadmcli container)."""

import json
import subprocess
from typing import Optional


class AS400JournalReader:
    """Read AS400 journal entries via qadmcli."""
    
    def __init__(self, qadmcli_path: str = "../qadmcli/qadmcli.sh"):
        self.qadmcli_path = qadmcli_path
    
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
            # Try to parse as JSON if possible
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {'output': result.stdout, 'success': True}
        except subprocess.CalledProcessError as e:
            return {'error': e.stderr or e.stdout, 'success': False}
    
    def get_changes(self, table: str, since: str) -> dict:
        """
        Get journal changes for a table since a timestamp.
        
        Args:
            table: Table name in format "LIBRARY.TABLE"
            since: Timestamp in format "YYYY-MM-DD HH:MM:SS"
            
        Returns:
            Dict with 'total', 'inserts', 'updates', 'deletes', 'entries'
        """
        # Parse library and table
        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format LIBRARY.TABLE, got: {table}")
        
        library, table_name = parts
        
        # Call qadmcli to get journal entries
        # This is a placeholder - actual implementation depends on qadmcli interface
        result = self._run_qadmcli(
            "journal", "entries",
            "-n", table_name,
            "-l", library,
            "--since", since,
            "--format", "json"
        )
        
        if not result.get('success', True):
            raise RuntimeError(f"qadmcli failed: {result.get('error')}")
        
        # Parse and categorize entries
        entries = result.get('entries', [])
        
        inserts = sum(1 for e in entries if e.get('entry_type') in ['PT', 'INSERT'])
        updates = sum(1 for e in entries if e.get('entry_type') in ['UP', 'UPDATE'])
        deletes = sum(1 for e in entries if e.get('entry_type') in ['DL', 'DELETE'])
        
        return {
            'total': len(entries),
            'inserts': inserts,
            'updates': updates,
            'deletes': deletes,
            'entries': entries[:100]  # Limit for performance
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
