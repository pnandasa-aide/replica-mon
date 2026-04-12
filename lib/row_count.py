"""Row count comparison fallback when CT is not enabled."""

import subprocess
import json
from typing import Optional


def run_qadmcli(*args) -> dict:
    """Run qadmcli command and return parsed output."""
    qadmcli_path = "../qadmcli/qadmcli.sh"
    cmd = [qadmcli_path] + list(args)
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


def get_as400_row_count(table: str) -> int:
    """Get row count from AS400 table."""
    # table format: LIBRARY.TABLE
    parts = table.split('.')
    if len(parts) != 2:
        raise ValueError(f"Table must be in format LIBRARY.TABLE, got: {table}")
    
    library, table_name = parts
    
    result = run_qadmcli(
        "sql", "execute",
        "-q", f"SELECT COUNT(*) as ROW_COUNT FROM {library}.{table_name}"
    )
    
    if 'error' in result:
        raise Exception(f"AS400 query failed: {result['error']}")
    
    # Parse the output to get count
    output = result.get('output', '')
    for line in output.split('\n'):
        if line.strip().isdigit():
            return int(line.strip())
    
    return 0


def get_mssql_row_count(table: str) -> int:
    """Get row count from MSSQL table."""
    # table format: SCHEMA.TABLE
    parts = table.split('.')
    if len(parts) != 2:
        raise ValueError(f"Table must be in format SCHEMA.TABLE, got: {table}")
    
    schema, table_name = parts
    
    result = run_qadmcli(
        "mssql", "query",
        "-q", f"SELECT COUNT(*) as ROW_COUNT FROM [{schema}].[{table_name}]",
        "--format", "json"
    )
    
    if 'error' in result:
        raise Exception(f"MSSQL query failed: {result['error']}")
    
    # Parse JSON output
    rows = result.get('rows', [])
    if rows:
        return int(rows[0].get('ROW_COUNT', 0))
    
    return 0


def compare_row_counts(source_table: str, target_table: str) -> dict:
    """
    Compare row counts between source and target tables.
    
    Args:
        source_table: AS400 table in format "LIBRARY.TABLE"
        target_table: MSSQL table in format "SCHEMA.TABLE"
        
    Returns:
        Dict with source_count, target_count, difference, match
    """
    try:
        source_count = get_as400_row_count(source_table)
    except Exception as e:
        print(f"  ✗ Error getting AS400 row count: {e}")
        source_count = 0
    
    try:
        target_count = get_mssql_row_count(target_table)
    except Exception as e:
        print(f"  ✗ Error getting MSSQL row count: {e}")
        target_count = 0
    
    difference = source_count - target_count
    
    return {
        'source_count': source_count,
        'target_count': target_count,
        'difference': difference,
        'match': source_count == target_count
    }
