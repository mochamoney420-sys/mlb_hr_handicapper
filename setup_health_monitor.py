#!/usr/bin/env python
"""One-time setup for auto-healing health monitor."""

import subprocess
import sys
from pathlib import Path

def setup_health_monitoring():
    """Initialize automatic daily health monitoring."""
    print("\n" + "="*70)
    print("MLB HR MODEL: AUTO-HEALING HEALTH MONITOR SETUP")
    print("="*70)
    
    print("\n📋 What this does:")
    print("  • Runs automatic health checks every 4 hours (7 AM → 11 PM)")
    print("  • Detects if prediction pipeline crashed or live monitor stopped")
    print("  • Automatically restarts failed processes")
    print("  • Logs all issues + fixes to data/recovery_log.txt")
    print("  • Validates Python environment + dependencies")
    print("  • Runs 24/7 without manual intervention")
    
    print("\n🔧 Installing...\n")
    
    # Run health monitor setup
    try:
        result = subprocess.run(
            [sys.executable, 'health_monitor.py', '--setup'],
            capture_output=True,
            text=True
        )
        
        print(result.stdout)
        if result.stderr:
            print(result.stderr)
        
        if result.returncode == 0:
            print("\n" + "="*70)
            print("✅ SETUP COMPLETE")
            print("="*70)
            print("\n📊 Monitor is now active:")
            print("  • Health checks: Every 4 hours (7 AM, 11 AM, 3 PM, 7 PM, 11 PM)")
            print("  • Status file: data/system_health.json")
            print("  • Recovery log: data/recovery_log.txt")
            print("\n🎯 To check status manually:")
            print("  python health_monitor.py")
            print("\n🚨 To view recovery attempts:")
            print("  type data\\recovery_log.txt")
            print("\n" + "="*70)
            return True
        else:
            print("\n❌ Setup failed. Try running with admin privileges:")
            print("  python health_monitor.py --setup")
            return False
            
    except Exception as e:
        print(f"❌ Error during setup: {e}")
        return False

if __name__ == '__main__':
    success = setup_health_monitoring()
    sys.exit(0 if success else 1)
