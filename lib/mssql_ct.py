"""MSSQL Change Tracking reader."""

import json
import subprocess
from typing import Optional


class MSSQLCTReader:
    """Read MSSQL Change Tracking data via qadmcli."""
    
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
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {'output': result.stdout, 'success': True}
        except subprocess.CalledProcessError as e:
            return {'error': e.stderr or e.stdout, 'success': False}
    
    def get_summary(self, table: str, since: Optional[str] = None) -> dict:
        """
        Get Change Tracking summary for comparison with AS400 journal.
        
        Args:
            table: Table name in format "SCHEMA.TABLE"
            since: Optional timestamp in format "YYYY-MM-DD HH:MM:SS"
            
        Returns:
            Dict with 'table', 'total', 'inserts', 'updates', 'deletes', 'changes'
        """
        parts = table.split('.')
        if len(parts) != 2:
            raise ValueError(f"Table must be in format SCHEMA.TABLE, got: {table}")
        
        schema, table_name = parts
        
        # Call qadmcli with summary format
        cmd_args = [
            "mssql", "ct", "changes",
            "-t", table_name,
            "-s", schema,
            "--format", "summary"
        ]
        
        if since:
            cmd_args.extend(["--since", since])
        
        result = self._run_qadmcli(*cmd_args)
        
        if 'error' in result:
            return {
                'table': table,
                'total': 0,
                'inserts': 0,
                'updates': 0,
                'deletes': 0,
                'changes': [],
                'error': result.get('error', 'Unknown error')
            }
        
        return result
    
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
            "-s", schema
        )
        
        if 'error' in result:
            return False
        
        return result.get('ct_enabled_on_table', False) or result.get('is_enabled_on_table', False)
