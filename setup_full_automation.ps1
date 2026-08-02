# MLB HR Handicapper - One-click automation bootstrap
# Usage (Admin PowerShell recommended):
#   powershell -ExecutionPolicy Bypass -File .\setup_full_automation.ps1

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source

if (-not $PythonExe) {
    Write-Error "Python not found in PATH. Install Python 3.14+ or activate your environment first."
    exit 1
}

Write-Host "Project dir : $ProjectDir"
Write-Host "Python      : $PythonExe"

# 1) Register/refresh all scheduled tasks for market-release operation.
Write-Host "Registering scheduled tasks..."
& powershell -ExecutionPolicy Bypass -File (Join-Path $ProjectDir "schedule_tasks.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Error "schedule_tasks.ps1 failed. Fix task registration errors and rerun."
    exit 1
}

# 2) Run a quick logic-only confidence regression check.
Write-Host "Running confidence regression tests..."
& $PythonExe -m unittest test_confidence_labels.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "Confidence tests failed. Do not enable auto execution yet."
    exit 1
}

# 3) Run a CLV audit command to verify the path is callable.
Write-Host "Running CLV audit command sanity check..."
& $PythonExe run_daily_predictions.py --audit-clv --audit-clv-days 30

Write-Host ""
Write-Host "Automation bootstrap complete."
Write-Host "Next: keep AUTO_WAGER_DRY_RUN=true until market monitor logs look correct."
