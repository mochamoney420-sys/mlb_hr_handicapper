# MLB HR Handicapper - Windows Task Scheduler Setup
# Run this script once as Administrator to register all scheduled tasks.
# Usage: powershell -ExecutionPolicy Bypass -File schedule_tasks.ps1

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source

if (-not $PythonExe) {
    Write-Error "Python not found in PATH. Install Python or activate your virtual environment first."
    exit 1
}

Write-Host "Project dir : $ProjectDir"
Write-Host "Python      : $PythonExe"

# ---------------------------------------------------------------
# TASK 1: Market-release predictions - runs at 9:05 AM every day
# Trains model, computes +EV edges, and sends "BET NOW" morning alert.
# ---------------------------------------------------------------
$action1 = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "run_daily_predictions.py" `
    -WorkingDirectory $ProjectDir

$trigger1 = New-ScheduledTaskTrigger -Daily -At "9:05AM"

$settings1 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName "MLB_HR_DailyPredictions" `
    -Action $action1 `
    -Trigger $trigger1 `
    -Settings $settings1 `
    -Description "Run pre-market MLB HR predictions and send morning BET NOW alert to Discord." `
    -Force

Write-Host "Registered: MLB_HR_DailyPredictions (daily at 9:05 AM)"

# ---------------------------------------------------------------
# TASK 2: Market monitor - runs at 9:10 AM every day
# Watches odds/line movement through the day for +EV and execution.
# ---------------------------------------------------------------
$action2 = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "run_daily_predictions.py --rlm" `
    -WorkingDirectory $ProjectDir

$trigger2 = New-ScheduledTaskTrigger -Daily -At "9:10AM"

$settings2 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName "MLB_HR_MarketMonitor" `
    -Action $action2 `
    -Trigger $trigger2 `
    -Settings $settings2 `
    -Description "Monitor live sportsbook markets (+EV, RLM, stale follower lag, optional auto execution)." `
    -Force

Write-Host "Registered: MLB_HR_MarketMonitor (daily at 9:10 AM, max 12h)"

# ---------------------------------------------------------------
# TASK 3: Live HR watcher - starts at 12:00 PM, runs up to 12 hours
# Watches live games and sends real-time HR alerts to Discord
# ---------------------------------------------------------------
$action3 = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "run_daily_predictions.py --live" `
    -WorkingDirectory $ProjectDir

$trigger3 = New-ScheduledTaskTrigger -Daily -At "12:00PM"

$settings3 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable

Register-ScheduledTask `
    -TaskName "MLB_HR_LiveWatcher" `
    -Action $action3 `
    -Trigger $trigger3 `
    -Settings $settings3 `
    -Description "Live MLB HR watcher - sends real-time Discord alerts." `
    -Force

Write-Host "Registered: MLB_HR_LiveWatcher (daily at 12:00 PM, max 12h)"

# ---------------------------------------------------------------
# TASK 4: Evaluation - runs at 11:45 PM every day
# Compares saved predictions to actual Statcast results, notifies Discord
# ---------------------------------------------------------------
$action4 = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "run_daily_predictions.py --evaluate --notify-eval" `
    -WorkingDirectory $ProjectDir

$trigger4 = New-ScheduledTaskTrigger -Daily -At "11:45PM"

$settings4 = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName "MLB_HR_EvaluatePredictions" `
    -Action $action4 `
    -Trigger $trigger4 `
    -Settings $settings4 `
    -Description "Evaluate MLB HR predictions vs actual results and notify Discord." `
    -Force

Write-Host "Registered: MLB_HR_EvaluatePredictions (daily at 11:45 PM)"

# ---------------------------------------------------------------
# Summary
# ---------------------------------------------------------------
Write-Host ""
Write-Host "All tasks registered. View or edit them in Task Scheduler (taskschd.msc)."
Write-Host ""
Write-Host "Scheduled tasks summary:"
Get-ScheduledTask | Where-Object { $_.TaskName -like "MLB_HR_*" } |
Select-Object TaskName, State | Format-Table -AutoSize
