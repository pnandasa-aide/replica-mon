"""
Timezone utilities for cross-database timestamp handling.

AS400 uses UTC+0, MSSQL uses UTC+7 (Asia/Bangkok).
This module provides automatic detection and conversion.
"""

import subprocess
import json
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple


def detect_as400_timezone(qadmcli_path: str = "../qadmcli/qadmcli.sh") -> int:
    """
    Detect AS400 timezone offset from UTC.
    
    Returns:
        Timezone offset in hours (e.g., 0 for UTC, 7 for UTC+7)
    """
    try:
        cmd = [
            qadmcli_path,
            "sql", "execute",
            "-q", "SELECT CURRENT TIMEZONE as TZ FROM SYSIBM.SYSDUMMY1",
            "--format", "json"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15)
        
        # Extract JSON from output
        output = result.stdout
        match = re.search(r'\[\s*\{\s*"TZ"\s*:\s*(\d+)\s*\}\s*\]', output, re.DOTALL)
        if match:
            # AS400 returns timezone as HHMMSS format (e.g., 70000 = +7:00:00)
            tz_value = int(match.group(1))
            hours = tz_value // 10000
            return hours
        
        return 0  # Default to UTC
    except Exception:
        return 0  # Default to UTC on error


def detect_mssql_timezone(qadmcli_path: str = "../qadmcli/qadmcli.sh") -> int:
    """
    Detect MSSQL timezone offset from UTC.
    
    Returns:
        Timezone offset in hours (e.g., 7 for UTC+7)
    """
    try:
        cmd = [
            qadmcli_path,
            "mssql", "execute",
            "-q", "SELECT DATEDIFF(hour, SYSUTCDATETIME(), SYSDATETIME()) as TZ_OFFSET",
            "--format", "json"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15)
        
        # Extract JSON from output
        output = result.stdout
        match = re.search(r'\[\s*\{\s*"TZ_OFFSET"\s*:\s*(-?\d+)\s*\}\s*\]', output, re.DOTALL)
        if match:
            return int(match.group(1))
        
        return 7  # Default to UTC+7
    except Exception:
        return 7  # Default to UTC+7 on error


def convert_timestamp(
    timestamp: str,
    from_tz_offset: int,
    to_tz_offset: int
) -> str:
    """
    Convert timestamp from one timezone to another.
    
    Args:
        timestamp: Timestamp string in format "YYYY-MM-DD HH:MM:SS"
        from_tz_offset: Source timezone offset from UTC (hours)
        to_tz_offset: Target timezone offset from UTC (hours)
        
    Returns:
        Converted timestamp string
    """
    if not timestamp:
        return timestamp
    
    try:
        # Parse timestamp
        dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        
        # Calculate offset difference
        tz_diff = to_tz_offset - from_tz_offset
        
        # Convert
        converted = dt + timedelta(hours=tz_diff)
        
        return converted.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        # If parsing fails, return original
        return timestamp


def normalize_to_as400_time(
    timestamp: str,
    mssql_tz_offset: int,
    as400_tz_offset: int = 0
) -> str:
    """
    Convert timestamp from MSSQL timezone to AS400 timezone.
    
    Args:
        timestamp: User-provided timestamp (assumed to be in MSSQL timezone)
        mssql_tz_offset: MSSQL timezone offset from UTC
        as400_tz_offset: AS400 timezone offset from UTC (default 0)
        
    Returns:
        Timestamp converted to AS400 timezone
    """
    return convert_timestamp(timestamp, mssql_tz_offset, as400_tz_offset)


def normalize_to_mssql_time(
    timestamp: str,
    as400_tz_offset: int = 0,
    mssql_tz_offset: int = 7
) -> str:
    """
    Convert timestamp from AS400 timezone to MSSQL timezone.
    
    Args:
        timestamp: AS400 timestamp
        as400_tz_offset: AS400 timezone offset from UTC (default 0)
        mssql_tz_offset: MSSQL timezone offset from UTC (default 7)
        
    Returns:
        Timestamp converted to MSSQL timezone
    """
    return convert_timestamp(timestamp, as400_tz_offset, mssql_tz_offset)


def get_timezone_info(qadmcli_path: str = "../qadmcli/qadmcli.sh") -> dict:
    """
    Get comprehensive timezone information for all systems.
    
    Returns:
        Dictionary with timezone info for local, AS400, and MSSQL
    """
    local_tz = datetime.now().astimezone().tzinfo
    local_offset = datetime.now().astimezone().utcoffset()
    local_hours = local_offset.total_seconds() // 3600 if local_offset else 0
    
    as400_offset = detect_as400_timezone(qadmcli_path)
    mssql_offset = detect_mssql_timezone(qadmcli_path)
    
    return {
        'local': {
            'timezone': str(local_tz) if local_tz else 'Unknown',
            'utc_offset': int(local_hours),
            'current_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        },
        'as400': {
            'timezone': f'UTC+{as400_offset}' if as400_offset >= 0 else f'UTC{as400_offset}',
            'utc_offset': as400_offset,
            'current_time': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")  # AS400 is UTC
        },
        'mssql': {
            'timezone': f'UTC+{mssql_offset}' if mssql_offset >= 0 else f'UTC{mssql_offset}',
            'utc_offset': mssql_offset,
            'current_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # MSSQL is local
        },
        'mismatch_detected': as400_offset != mssql_offset,
        'offset_difference_hours': mssql_offset - as400_offset
    }


def format_timezone_report(tz_info: dict) -> str:
    """
    Format timezone information for display in report header.
    
    Args:
        tz_info: Timezone info dictionary from get_timezone_info()
        
    Returns:
        Formatted string for display
    """
    lines = [
        "",
        "Timezone Information:",
        f"  Local System:  {tz_info['local']['timezone']} (UTC{tz_info['local']['utc_offset']:+d}) - {tz_info['local']['current_time']}",
        f"  AS400:         {tz_info['as400']['timezone']} - {tz_info['as400']['current_time']}",
        f"  MSSQL:         {tz_info['mssql']['timezone']} - {tz_info['mssql']['current_time']}",
    ]
    
    if tz_info['mismatch_detected']:
        lines.append("")
        lines.append(f"  ⚠️  Timezone mismatch detected: {tz_info['offset_difference_hours']} hour(s) difference")
        lines.append("  ℹ️  Timestamps will be automatically normalized for accurate comparison")
    
    lines.append("")
    return "\n".join(lines)
