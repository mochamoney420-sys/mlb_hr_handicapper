#!/usr/bin/env python
"""
Auto-healing health monitor: Detects and fixes crashes 24/7.
Runs every 2-4 hours, checks system health, auto-recovers from failures.
"""

import os
import sys
import json
import subprocess
import psutil
from datetime import datetime, timedelta
from pathlib import Path
import time

# Load environment
env_file = Path(__file__).parent / '.vscode' / '.env'
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, val = line.split('=', 1)
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            os.environ.setdefault(key.strip(), val.strip())

# =====================================================================
# HEALTH STATUS TRACKING
# =====================================================================

STATUS_FILE = Path(__file__).parent / 'data' / 'system_health.json'
RECOVERY_LOG = Path(__file__).parent / 'data' / 'recovery_log.txt'

def init_status_file():
    """Initialize or load current system status."""
    Path('data').mkdir(exist_ok=True)
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except:
            pass
    return {
        'last_check': None,
        'main_pipeline_running': False,
        'live_monitor_running': False,
        'last_main_run': None,
        'last_live_run': None,
        'crash_count': 0,
        'recovery_attempts': 0,
        'status': 'UNKNOWN'
    }

def save_status(status):
    """Persist health status to file."""
    status['last_check'] = datetime.now().isoformat()
    STATUS_FILE.write_text(json.dumps(status, indent=2))

def log_recovery(message):
    """Log recovery attempts to file."""
    with open(RECOVERY_LOG, 'a') as f:
        f.write(f"[{datetime.now().isoformat()}] {message}\n")
    print(f"🔧 {message}")

# =====================================================================
# PROCESS HEALTH CHECKS
# =====================================================================

def find_process_by_script(script_name):
    """Find Python process running specific script."""
    target_script = script_name.replace('/', '\\')
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['name'] == 'python.exe' or proc.info['name'] == 'python':
                    cmdline = ' '.join(proc.info['cmdline'] or [])
                    if target_script in cmdline:
                        return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except:
        pass
    return None

def check_main_pipeline_health():
    """Verify main prediction pipeline ran recently."""
    # Check if predictions CSV was generated today
    today = datetime.today().strftime('%Y-%m-%d')
    pred_file = Path('data') / f'predictions_{today}.csv'
    
    if pred_file.exists():
        age_minutes = (datetime.now() - datetime.fromtimestamp(pred_file.stat().st_mtime)).total_seconds() / 60
        return age_minutes < 1440, age_minutes  # Less than 24 hours old
    
    return False, None

def check_live_monitor_health():
    """Verify live monitor is running."""
    proc = find_process_by_script('run_daily_predictions.py')
    if proc and '--live' in ' '.join(proc.cmdline() or []):
        return True, proc.pid
    return False, None

def check_python_environment():
    """Verify Python environment is valid and dependencies installed."""
    try:
        required = ['pandas', 'numpy', 'statsapi', 'requests']
        for module in required:
            __import__(module)
        return True, None
    except ImportError as e:
        return False, str(e)

# =====================================================================
# AUTO-RECOVERY PROCEDURES
# =====================================================================

def restart_live_monitor():
    """Restart the live HR monitor."""
    log_recovery("Attempting to restart live HR monitor...")
    try:
        # Kill existing monitor if running
        proc = find_process_by_script('run_daily_predictions.py')
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except:
                proc.kill()
            time.sleep(2)
        
        # Spawn new monitor
        subprocess.Popen(
            [sys.executable, 'run_daily_predictions.py', '--live'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
        time.sleep(2)
        
        # Verify it started
        if find_process_by_script('run_daily_predictions.py'):
            log_recovery("✅ Live monitor restarted successfully")
            return True
        else:
            log_recovery("❌ Live monitor failed to start")
            return False
    except Exception as e:
        log_recovery(f"❌ Failed to restart monitor: {e}")
        return False

def trigger_daily_predictions():
    """Run daily predictions pipeline."""
    log_recovery("Triggering daily predictions pipeline...")
    try:
        result = subprocess.run(
            [sys.executable, 'run_daily_predictions.py'],
            capture_output=True,
            timeout=600,
            text=True
        )
        if result.returncode == 0:
            log_recovery("✅ Daily predictions completed successfully")
            return True
        else:
            log_recovery(f"❌ Predictions failed: {result.stderr[:200]}")
            return False
    except subprocess.TimeoutExpired:
        log_recovery("❌ Predictions timed out (>10 min)")
        return False
    except Exception as e:
        log_recovery(f"❌ Failed to run predictions: {e}")
        return False

def repair_environment():
    """Attempt to repair Python environment."""
    log_recovery("Attempting environment repair...")
    try:
        # Re-run pip install on requirements
        if Path('requirements.txt').exists():
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt', '-q'],
                capture_output=True,
                timeout=120
            )
            if result.returncode == 0:
                log_recovery("✅ Dependencies verified/repaired")
                return True
    except Exception as e:
        log_recovery(f"⚠️  Could not repair environment: {e}")
    return False

# =====================================================================
# COMPREHENSIVE HEALTH CHECK
# =====================================================================

def run_health_check():
    """Execute full system health check and auto-recovery."""
    print("\n" + "="*70)
    print(f"HEALTH CHECK: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)
    
    status = init_status_file()
    issues = []
    fixes_applied = 0
    
    # Check 1: Python Environment
    print("\n[1/4] Checking Python environment...")
    env_ok, env_err = check_python_environment()
    if not env_ok:
        print(f"  ❌ Missing dependency: {env_err}")
        issues.append(f"Missing dependency: {env_err}")
        if repair_environment():
            fixes_applied += 1
    else:
        print("  ✅ Environment OK")
    
    # Check 2: Main Pipeline Recent Activity
    print("\n[2/4] Checking main pipeline (daily predictions)...")
    pipeline_ok, age = check_main_pipeline_health()
    if not pipeline_ok:
        print("  ⚠️  No recent predictions found")
        issues.append("Missing daily predictions")
        if trigger_daily_predictions():
            fixes_applied += 1
    else:
        print(f"  ✅ Recent predictions ({int(age)} min old)")
    
    # Check 3: Live Monitor
    print("\n[3/4] Checking live HR monitor...")
    monitor_ok, pid = check_live_monitor_health()
    if not monitor_ok:
        print("  ❌ Live monitor not running")
        issues.append("Live monitor crashed")
        if restart_live_monitor():
            fixes_applied += 1
        else:
            status['crash_count'] += 1
    else:
        print(f"  ✅ Monitor running (PID {pid})")
    
    # Check 4: Disk Space & Permissions
    print("\n[4/4] Checking disk space & file permissions...")
    try:
        import shutil
        stat = shutil.disk_usage('.')
        free_gb = stat.free / (1024**3)
        if free_gb < 0.5:
            print(f"  ⚠️  Low disk space ({free_gb:.1f} GB)")
            issues.append("Low disk space")
        else:
            print(f"  ✅ Disk space OK ({free_gb:.1f} GB free)")
    except Exception as e:
        print(f"  ⚠️  Could not check disk: {e}")
    
    # Summary
    print("\n" + "="*70)
    status['status'] = 'HEALTHY' if not issues else 'DEGRADED'
    status['main_pipeline_running'] = pipeline_ok
    status['live_monitor_running'] = monitor_ok
    status['recovery_attempts'] += fixes_applied
    
    if issues:
        print(f"⚠️  {len(issues)} issues detected, {fixes_applied} auto-fixes applied")
        for issue in issues:
            print(f"  • {issue}")
    else:
        print("✅ ALL SYSTEMS HEALTHY")
    
    print(f"   Status: {status['status']} | Crashes: {status['crash_count']} | Recoveries: {status['recovery_attempts']}")
    print("="*70 + "\n")
    
    save_status(status)
    return len(issues) == 0

# =====================================================================
# SCHEDULED MONITORING
# =====================================================================

def schedule_health_checks():
    """Create Windows Task Scheduler job for automatic checks."""
    import subprocess
    
    script_path = Path(__file__).resolve()
    workspace_dir = script_path.parent
    
    # PowerShell script to create scheduled task
    ps_cmd = f"""
$TaskName = "MLB_HR_HealthMonitor"
$TaskPath = "\\MLB_HR_Handicapper\\"

# Check if task exists
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($task) {{
    Write-Host "Task already exists. Skipping..."
    exit 0
}}

# Create trigger: Every 4 hours, starting at 7 AM
$trigger = New-ScheduledTaskTrigger -Daily -At 7:00am -RepetitionInterval (New-TimeSpan -Hours 4) -RepetitionDuration (New-TimeSpan -Days 999)

# Create action: Run Python script
$action = New-ScheduledTaskAction -Execute 'C:\\Users\\bobby\\AppData\\Local\\Programs\\Python\\Python314\\python.exe' -Argument '{script_path} --auto' -WorkingDirectory '{workspace_dir}'

# Create task with high priority
$task = New-ScheduledTask -Action $action -Trigger $trigger -TaskName $TaskName -Description "MLB HR Model: Auto health monitoring and recovery" -RunLevel Highest

# Register task
Register-ScheduledTask -TaskPath $TaskPath -InputObject $task -Force
Write-Host "✅ Health monitor scheduled (every 4 hours starting 7 AM)"
"""
    
    try:
        result = subprocess.run(
            ['powershell', '-Command', ps_cmd],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            print("✅ Scheduled health checks activated")
            print("   • Runs every 4 hours starting at 7:00 AM")
            print("   • Auto-detects and fixes crashes")
            print("   • Logs all recovery attempts")
            return True
        else:
            print(f"⚠️  Could not schedule: {result.stderr}")
            return False
    except Exception as e:
        print(f"⚠️  Scheduling error: {e}")
        return False

# =====================================================================
# ENTRY POINT
# =====================================================================

if __name__ == '__main__':
    if '--setup' in sys.argv:
        # One-time setup: create scheduled task
        print("Setting up automatic health monitoring...")
        schedule_health_checks()
    elif '--auto' in sys.argv:
        # Called by scheduler: run check silently
        run_health_check()
    else:
        # Manual check: run with full output
        success = run_health_check()
        sys.exit(0 if success else 1)
