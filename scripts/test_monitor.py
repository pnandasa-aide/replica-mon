#!/usr/bin/env python3
"""
Test Monitor Script - Generate Mockup Data & Verify Replication

Generates mockup data for specified tables and automatically runs
replication comparison reports to verify CDC is working correctly.

Usage:
    python test_monitor.py
    python test_monitor.py --tables CUSTOMERS,CUSTOMERS2
    python test_monitor.py --tables ORDERS --rows 20
    python test_monitor.py --format json --output report.json
"""

import subprocess
import sys
import time
import json
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Try to import compare module
try:
    from compare import generate_report
except ImportError:
    print("⚠️  Warning: Could not import compare module")
    print("   Will fallback to running compare.py as subprocess")
    HAS_COMPARE_MODULE = False
else:
    HAS_COMPARE_MODULE = True


class TestMonitor:
    """Automated test monitor for replication verification."""
    
    def __init__(
        self,
        tables: List[str] = None,
        rows_per_table: int = 10,
        output_format: str = "text",
        output_file: str = None,
        wait_seconds: int = 60,
        library: str = "GSLIBTST",
        target_schema: str = "dbo",
        base_dir: str = None
    ):
        """
        Initialize test monitor.
        
        Args:
            tables: List of table names to test
            rows_per_table: Number of mockup rows to generate per table
            output_format: Report format (text or json)
            output_file: Optional file to save report
            wait_seconds: Seconds to wait for CDC replication
            library: AS400 library name
            target_schema: MSSQL target schema
            base_dir: Base directory for finding other tools
        """
        self.tables = tables or ["CUSTOMERS", "CUSTOMERS2", "ORDERS"]
        self.rows_per_table = rows_per_table
        self.output_format = output_format
        self.output_file = output_file
        self.wait_seconds = wait_seconds
        self.library = library
        self.target_schema = target_schema
        
        # Determine base directory (parent of replica-mon)
        if base_dir is None:
            # Default: go up 2 levels from scripts/ -> replica-mon/ -> _qoder/
            self.base_dir = str(Path(__file__).parent.parent.parent)
        else:
            self.base_dir = base_dir
        
        # Tool paths
        self.qadmcli_path = os.path.join(self.base_dir, "qadmcli", "qadmcli.sh")
        self.compare_script = os.path.join(self.base_dir, "replica-mon", "compare.py")
        
        # Results storage
        self.results = {
            "timestamp": datetime.now().isoformat(),
            "tables": {},
            "summary": {
                "total_tables": len(self.tables),
                "successful": 0,
                "failed": 0,
                "warnings": 0
            }
        }
        
    def run_command(self, cmd: List[str], description: str = "") -> dict:
        """Run shell command and return result."""
        if description:
            print(f"\n{'='*70}")
            print(f"  {description}")
            print(f"{'='*70}")
        
        print(f"Command: {' '.join(cmd)}")
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=300
            )
            return {
                "success": True,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode
            }
        except subprocess.CalledProcessError as e:
            return {
                "success": False,
                "stdout": e.stdout,
                "stderr": e.stderr,
                "returncode": e.returncode,
                "error": str(e)
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": "Command timed out after 300 seconds"
            }
    
    def get_table_mapping(self, table_name: str) -> Dict[str, str]:
        """Get AS400 and MSSQL table paths for a table name."""
        return {
            "name": table_name,
            "source": f"{self.library}.{table_name}",
            "target": f"{self.target_schema}.{table_name.lower()}"
        }
    
    def generate_mockup_data(self, table_name: str) -> dict:
        """Generate mockup data for a table."""
        print(f"\n{'─'*70}")
        print(f"  Step 1: Generating mockup data for {table_name}")
        print(f"{'─'*70}")
        
        cmd = [
            self.qadmcli_path,
            "mockup", "generate",
            "-t", table_name,
            "-l", self.library,
            "-n", str(self.rows_per_table)
        ]
        
        result = self.run_command(cmd, f"Generating {self.rows_per_table} rows for {table_name}")
        
        if result["success"]:
            print(f"✅ Mockup data generated successfully")
            # Parse output to get insert/update/delete counts
            output = result["stdout"]
            stats = self._parse_mockup_output(output)
            return {
                "success": True,
                "stats": stats,
                "output": output
            }
        else:
            print(f"❌ Mockup generation failed: {result.get('error', 'Unknown error')}")
            if result.get('stderr'):
                print(f"   Error: {result['stderr'][:200]}")
            return {
                "success": False,
                "stats": {},
                "error": result.get('error', result.get('stderr', 'Unknown error'))
            }
    
    def _parse_mockup_output(self, output: str) -> dict:
        """Parse mockup generation output for statistics."""
        stats = {"inserts": 0, "updates": 0, "deletes": 0}
        
        for line in output.split('\n'):
            line = line.strip()
            if "INSERT" in line and "statements" in line:
                try:
                    stats["inserts"] = int(line.split()[0])
                except:
                    pass
            elif "UPDATE" in line and "statements" in line:
                try:
                    stats["updates"] = int(line.split()[0])
                except:
                    pass
            elif "DELETE" in line and "statements" in line:
                try:
                    stats["deletes"] = int(line.split()[0])
                except:
                    pass
        
        return stats
    
    def wait_for_replication(self, table_name: str):
        """Wait for CDC replication to complete."""
        print(f"\n{'─'*70}")
        print(f"  Step 2: Waiting {self.wait_seconds}s for CDC replication...")
        print(f"{'─'*70}")
        print(f"  (CDC typically replicates within 30-60 seconds)")
        print(f"  Table: {table_name}")
        
        for i in range(self.wait_seconds, 0, -10):
            print(f"  ⏱️  {i} seconds remaining...", end='\r', flush=True)
            time.sleep(10)
        
        print(f"\n  ✅ Wait complete - checking replication now")
    
    def check_replication(self, table_mapping: dict) -> dict:
        """Check replication for a table using compare module."""
        print(f"\n{'─'*70}")
        print(f"  Step 3: Checking replication for {table_mapping['name']}")
        print(f"{'─'*70}")
        print(f"  Source: {table_mapping['source']}")
        print(f"  Target: {table_mapping['target']}")
        
        if HAS_COMPARE_MODULE:
            # Use module directly
            try:
                # Capture output
                import io
                from contextlib import redirect_stdout
                
                f = io.StringIO()
                with redirect_stdout(f):
                    generate_report(
                        source_table=table_mapping['source'],
                        target_table=table_mapping['target'],
                        output_format=self.output_format
                    )
                
                output = f.getvalue()
                print(output)
                
                return {
                    "success": True,
                    "output": output,
                    "method": "module"
                }
            except Exception as e:
                print(f"  ⚠️  Module comparison failed: {e}")
                print(f"  Falling back to subprocess...")
        
        # Fallback to subprocess
        cmd = [
            "python3", self.compare_script,
            "--source", table_mapping['source'],
            "--target", table_mapping['target'],
            "--format", self.output_format
        ]
        
        result = self.run_command(cmd, "Running replication comparison")
        
        if result["success"]:
            return {
                "success": True,
                "output": result["stdout"],
                "method": "subprocess"
            }
        else:
            return {
                "success": False,
                "error": result.get('error', result.get('stderr', 'Unknown error')),
                "method": "subprocess"
            }
    
    def get_row_counts(self, table_mapping: dict) -> dict:
        """Get row counts from both source and target."""
        print(f"\n{'─'*70}")
        print(f"  Row Count Verification")
        print(f"{'─'*70}")
        
        # AS400 count - use --format json for clean output
        cmd_source = [
            self.qadmcli_path,
            "sql", "execute",
            "-q", f"SELECT COUNT(*) as CNT FROM {table_mapping['source']}",
            "--format", "json"  # Use JSON for easy parsing
        ]
        
        result_source = self.run_command(cmd_source, "Counting AS400 rows")
        source_count = 0
        if result_source["success"]:
            try:
                # Extract JSON from output (may have shell wrapper messages)
                import re
                output = result_source["stdout"]
                
                # Find JSON array pattern
                match = re.search(r'\[\s*\{\s*"CNT"\s*:\s*(\d+)\s*\}\s*\]', output, re.DOTALL)
                if match:
                    source_count = int(match.group(1))
            except Exception as e:
                print(f"  ⚠️  Warning: Could not parse AS400 count: {e}")
        
        # MSSQL count - use subprocess with better JSON extraction
        target_parts = table_mapping['target'].split('.')
        cmd_target = [
            self.qadmcli_path,
            "mssql", "query",
            "-q", f"SELECT COUNT(*) as CNT FROM [{target_parts[0]}].[{target_parts[1]}]",
            "--format", "json"
        ]
        
        result_target = self.run_command(cmd_target, "Counting MSSQL rows")
        target_count = 0
        if result_target["success"]:
            try:
                # Extract JSON from output (may have Rich formatting mixed in)
                import re
                output = result_target["stdout"]
                
                # Find JSON array pattern: [\n  {\n    "CNT": 66\n  }\n]
                match = re.search(r'\[\s*\{\s*"CNT"\s*:\s*(\d+)\s*\}\s*\]', output, re.DOTALL)
                if match:
                    target_count = int(match.group(1))
            except Exception as e:
                print(f"  ⚠️  Warning: Could not parse MSSQL count: {e}")
        
        print(f"\n  {'Table':<25} {'Count':>10}")
        print(f"  {'─'*40}")
        print(f"  {'Source (AS400)':<25} {source_count:>10}")
        print(f"  {'Target (MSSQL)':<25} {target_count:>10}")
        
        diff = source_count - target_count
        match = diff == 0
        
        print(f"  {'─'*40}")
        print(f"  {'Difference':<25} {diff:>+10}")
        
        if match:
            print(f"\n  ✅ Row counts match!")
        else:
            print(f"\n  ⚠️  Row count mismatch: {diff} rows")
        
        return {
            "source_count": source_count,
            "target_count": target_count,
            "difference": diff,
            "match": match
        }
    
    def test_table(self, table_name: str) -> dict:
        """Run complete test for a single table."""
        print(f"\n{'#'*70}")
        print(f"# Testing Table: {table_name}")
        print(f"{'#'*70}")
        
        table_mapping = self.get_table_mapping(table_name)
        table_result = {
            "name": table_name,
            "mapping": table_mapping,
            "mockup": None,
            "replication": None,
            "row_counts": None,
            "status": "pending"
        }
        
        # Step 1: Generate mockup data
        mockup_result = self.generate_mockup_data(table_name)
        table_result["mockup"] = mockup_result
        
        if not mockup_result["success"]:
            table_result["status"] = "failed"
            self.results["summary"]["failed"] += 1
            print(f"\n❌ Test FAILED for {table_name} - mockup generation failed")
            return table_result
        
        # Step 2: Wait for replication
        self.wait_for_replication(table_name)
        
        # Step 3: Check replication
        replication_result = self.check_replication(table_mapping)
        table_result["replication"] = replication_result
        
        # Step 4: Verify row counts
        row_counts = self.get_row_counts(table_mapping)
        table_result["row_counts"] = row_counts
        
        # Determine status
        if replication_result["success"] and row_counts["match"]:
            table_result["status"] = "success"
            self.results["summary"]["successful"] += 1
            print(f"\n✅ Test PASSED for {table_name}")
        elif replication_result["success"]:
            table_result["status"] = "warning"
            self.results["summary"]["warnings"] += 1
            print(f"\n⚠️  Test COMPLETED WITH WARNINGS for {table_name}")
        else:
            table_result["status"] = "failed"
            self.results["summary"]["failed"] += 1
            print(f"\n❌ Test FAILED for {table_name}")
        
        return table_result
    
    def print_summary(self):
        """Print test summary."""
        print(f"\n{'='*70}")
        print(f"  TEST SUMMARY")
        print(f"{'='*70}")
        print(f"  Timestamp: {self.results['timestamp']}")
        print(f"  Tables Tested: {self.results['summary']['total_tables']}")
        print(f"  ✅ Successful: {self.results['summary']['successful']}")
        print(f"  ⚠️  Warnings: {self.results['summary']['warnings']}")
        print(f"  ❌ Failed: {self.results['summary']['failed']}")
        print(f"{'='*70}")
        
        print(f"\n{'Table':<20} {'Mockup':<12} {'Replication':<15} {'Row Counts':<12} {'Status':<10}")
        print(f"{'─'*70}")
        
        for table_name, table_data in self.results["tables"].items():
            mockup_status = "✅" if table_data["mockup"] and table_data["mockup"]["success"] else "❌"
            
            repl_status = "⏳"
            if table_data["replication"]:
                repl_status = "✅" if table_data["replication"]["success"] else "❌"
            
            row_status = "⏳"
            if table_data["row_counts"]:
                row_status = "✅" if table_data["row_counts"]["match"] else "⚠️"
            
            status_map = {
                "success": "✅ PASS",
                "warning": "⚠️  WARN",
                "failed": "❌ FAIL",
                "pending": "⏳ PENDING"
            }
            final_status = status_map.get(table_data["status"], "❓ UNKNOWN")
            
            print(f"{table_name:<20} {mockup_status:<12} {repl_status:<15} {row_status:<12} {final_status:<10}")
        
        print(f"{'─'*70}")
        
        # Overall result
        if self.results["summary"]["failed"] == 0:
            print(f"\n🎉 ALL TESTS PASSED!")
        else:
            print(f"\n⚠️  {self.results['summary']['failed']} TABLE(S) FAILED")
    
    def save_report(self):
        """Save report to file."""
        if not self.output_file:
            return
        
        print(f"\n💾 Saving report to {self.output_file}...")
        
        try:
            with open(self.output_file, 'w') as f:
                json.dump(self.results, f, indent=2)
            print(f"✅ Report saved successfully")
        except Exception as e:
            print(f"❌ Failed to save report: {e}")
    
    def validate_environment(self) -> bool:
        """Validate that all required tools and paths exist."""
        print(f"\n{'='*70}")
        print(f"  Environment Validation")
        print(f"{'='*70}")
        
        all_valid = True
        
        # Check qadmcli
        if os.path.isfile(self.qadmcli_path):
            print(f"  ✅ qadmcli: {self.qadmcli_path}")
        else:
            print(f"  ❌ qadmcli not found: {self.qadmcli_path}")
            all_valid = False
        
        # Check compare.py
        if os.path.isfile(self.compare_script):
            print(f"  ✅ compare.py: {self.compare_script}")
        else:
            print(f"  ❌ compare.py not found: {self.compare_script}")
            all_valid = False
        
        # Check base directory
        if os.path.isdir(self.base_dir):
            print(f"  ✅ Base directory: {self.base_dir}")
        else:
            print(f"  ❌ Base directory not found: {self.base_dir}")
            all_valid = False
        
        print(f"{'='*70}")
        
        if not all_valid:
            print(f"\n❌ Environment validation failed!")
            print(f"\nExpected structure:")
            print(f"  {self.base_dir}/")
            print(f"    ├── qadmcli/")
            print(f"    │   └── qadmcli.sh")
            print(f"    └── replica-mon/")
            print(f"        ├── compare.py")
            print(f"        └── scripts/")
            print(f"            └── test_monitor.py")
            print(f"\nYou can override the base directory with --base-dir option")
        
        return all_valid
    
    def run(self):
        """Run complete test suite in batch mode."""
        print(f"\n{'#'*70}")
        print(f"# REPLICATION TEST MONITOR (BATCH MODE)")
        print(f"{'#'*70}")
        print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Tables: {', '.join(self.tables)}")
        print(f"  Rows per table: {self.rows_per_table}")
        print(f"  CDC wait time: {self.wait_seconds}s (combined)")
        print(f"  Output format: {self.output_format}")
        print(f"{'#'*70}")
        
        # Validate environment first
        if not self.validate_environment():
            print(f"\n❌ Cannot proceed - environment validation failed")
            return 1
        
        # PHASE 1: Generate all mockup data
        print(f"\n{'='*70}")
        print(f"  PHASE 1: Generate Mockup Data for All Tables")
        print(f"{'='*70}")
        
        for table_name in self.tables:
            table_mapping = self.get_table_mapping(table_name)
            table_result = {
                "name": table_name,
                "mapping": table_mapping,
                "mockup": None,
                "replication": None,
                "row_counts": None,
                "status": "pending"
            }
            
            # Generate mockup data
            mockup_result = self.generate_mockup_data(table_name)
            table_result["mockup"] = mockup_result
            
            if not mockup_result["success"]:
                table_result["status"] = "failed"
                self.results["summary"]["failed"] += 1
                print(f"\n❌ Mockup generation FAILED for {table_name}")
            else:
                print(f"\n✅ Mockup generation succeeded for {table_name}")
            
            self.results["tables"][table_name] = table_result
        
        # Check if all mockups succeeded
        failed_mockups = [t for t in self.results["tables"].values() if t["status"] == "failed"]
        if failed_mockups:
            print(f"\n❌ {len(failed_mockups)} table(s) failed mockup generation - aborting")
            self.print_summary()
            return 1
        
        # PHASE 2: Wait for replication (single combined wait)
        print(f"\n{'='*70}")
        print(f"  PHASE 2: Waiting {self.wait_seconds}s for CDC replication...")
        print(f"{'='*70}")
        print(f"  Tables: {', '.join(self.tables)}")
        print(f"  (CDC typically replicates within 30-60 seconds)")
        
        for i in range(self.wait_seconds, 0, -10):
            print(f"  ⏱️  {i} seconds remaining...", end='\r', flush=True)
            time.sleep(10)
        
        print(f"\n  ✅ Wait complete - checking replication now")
        
        # PHASE 3: Check replication for all tables
        print(f"\n{'='*70}")
        print(f"  PHASE 3: Verify Replication for All Tables")
        print(f"{'='*70}")
        
        for table_name in self.tables:
            table_result = self.results["tables"][table_name]
            table_mapping = table_result["mapping"]
            
            print(f"\n{'─'*70}")
            print(f"  Checking: {table_name}")
            print(f"{'─'*70}")
            
            # Check replication
            replication_result = self.check_replication(table_mapping)
            table_result["replication"] = replication_result
            
            # Verify row counts
            row_counts = self.get_row_counts(table_mapping)
            table_result["row_counts"] = row_counts
            
            # Determine status
            if replication_result["success"] and row_counts["match"]:
                table_result["status"] = "success"
                self.results["summary"]["successful"] += 1
                print(f"\n✅ Test PASSED for {table_name}")
            elif replication_result["success"]:
                table_result["status"] = "warning"
                self.results["summary"]["warnings"] += 1
                print(f"\n⚠️  Test COMPLETED WITH WARNINGS for {table_name}")
            else:
                table_result["status"] = "failed"
                self.results["summary"]["failed"] += 1
                print(f"\n❌ Test FAILED for {table_name}")
        
        # Print summary
        self.print_summary()
        
        # Save report if requested
        if self.output_file:
            self.save_report()
        
        # Return exit code
        if self.results["summary"]["failed"] > 0:
            return 1
        return 0


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Test Monitor - Generate mockup data and verify replication",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test all three default tables (batch mode)
  python test_monitor.py
  
  # Test specific tables
  python test_monitor.py --tables CUSTOMERS,CUSTOMERS2
  
  # Generate 20 transactions per table, wait 60s
  python test_monitor.py --transactions 20
  
  # Quick test with fewer rows
  python test_monitor.py --rows 5 --wait 30
  
  # Save JSON report
  python test_monitor.py --format json --output report.json
        """
    )
    
    parser.add_argument(
        "--tables",
        type=str,
        default="CUSTOMERS,CUSTOMERS2,ORDERS",
        help="Comma-separated list of tables to test (default: CUSTOMERS,CUSTOMERS2,ORDERS)"
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=10,
        help="Number of mockup rows/transactions per table (default: 10)"
    )
    parser.add_argument(
        "--transactions",
        type=int,
        default=None,
        help="Number of transactions per table (alias for --rows)"
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=60,
        help="Seconds to wait for CDC replication (default: 60, combined for all tables)"
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)"
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Save report to file (JSON format)"
    )
    parser.add_argument(
        "--library",
        type=str,
        default="GSLIBTST",
        help="AS400 library name (default: GSLIBTST)"
    )
    parser.add_argument(
        "--schema",
        type=str,
        default="dbo",
        help="MSSQL target schema (default: dbo)"
    )
    parser.add_argument(
        "--base-dir",
        type=str,
        default=None,
        help="Base directory for finding tools (default: auto-detect)"
    )
    
    args = parser.parse_args()
    
    # Parse tables
    tables = [t.strip() for t in args.tables.split(',') if t.strip()]
    
    if not tables:
        print("❌ Error: No tables specified")
        sys.exit(1)
    
    # Use --transactions if provided, otherwise use --rows
    rows_per_table = args.transactions if args.transactions is not None else args.rows
    
    # Create and run monitor
    monitor = TestMonitor(
        tables=tables,
        rows_per_table=rows_per_table,
        output_format=args.format,
        output_file=args.output,
        wait_seconds=args.wait,
        library=args.library,
        target_schema=args.schema,
        base_dir=args.base_dir
    )
    
    exit_code = monitor.run()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
