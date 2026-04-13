#!/usr/bin/env python3
"""
Diagnostic script to test monitor.py entity checking with full error details.
"""

import sys
import json
import traceback
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent))

from monitor import get_entity_comparison

def diagnose_entity(source_table, target_table):
    """Test a single entity and show full diagnostics."""
    print(f"\n{'='*80}")
    print(f"Testing: {source_table} → {target_table}")
    print(f"{'='*80}\n")
    
    try:
        result = get_entity_comparison(
            source_table, 
            target_table,
            use_cache=False,
            verbose=True
        )
        
        print(f"\n{'='*80}")
        print(f"RESULT")
        print(f"{'='*80}")
        print(f"Status: {result.get('status')}")
        print(f"Journal Total: {result.get('journal_total', 'N/A')}")
        print(f"CT Total: {result.get('ct_total', 'N/A')}")
        
        if result.get('error'):
            print(f"\nERROR: {result['error']}")
            if result.get('traceback'):
                print(f"\nTRACEBACK:\n{result['traceback']}")
        
        print(f"\nFull Result:")
        print(json.dumps(result, indent=2, default=str))
        
    except Exception as e:
        print(f"\n❌ FATAL ERROR: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    # Test all three entities
    entities = [
        ("GSLIBTST.ORDERS", "dbo.ORDERS"),
        ("GSLIBTST.CUSTOMERS", "dbo.CUSTOMERS"),
        ("GSLIBTST.CUSTOMERS2", "dbo.CUSTOMERS2"),
    ]
    
    print("REPLICA-MON DIAGNOSTIC TOOL")
    print("="*80)
    
    for source, target in entities:
        diagnose_entity(source, target)
        print("\n" + "="*80)
