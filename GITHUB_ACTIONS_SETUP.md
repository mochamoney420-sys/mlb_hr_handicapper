# ✅ GITHUB ACTIONS AUTOMATION VERIFICATION

Your workflow is configured to run at **9:00 AM UTC daily**.

## Timezone Conversion

UTC 9:00 AM = Your Local Time:
- If you're in Eastern Time (ET):    4:00 AM ET
- If you're in Central Time (CT):    3:00 AM CT  
- If you're in Mountain Time (MT):   2:00 AM MT
- If you're in Pacific Time (PT):    1:00 AM PT

⚠️ **NOTE:** This might be too early! You probably want 9 AM ET (market open).

## To Enable/Verify GitHub Actions:

1. Go to: https://github.com/YOUR_USERNAME/mlb_hr_handicapper
2. Click "Actions" tab
3. Verify "Daily MLB HR Prediction Pipeline" is visible
4. Check "All workflows" shows green checkmarks (if it's run before)

## To Configure Discord Webhook in GitHub:

1. Go to Settings → Secrets and variables → Actions
2. Click "New repository secret"
3. Name: `DISCORD_WEBHOOK_URL`
4. Value: (Your webhook URL from .env)
5. Click "Add secret"

## What Runs in GitHub Actions:

✅ Checkout code
✅ Setup Python 3.10
✅ Install dependencies
✅ Run: python run_daily_predictions.py

This triggers:
  • PHASE 0: Analyze yesterday's HRs (auto-learning)
  • PHASE 1-4: Normal daily predictions
  • Live monitor spawned
  • Discord alerts sent

## What DOESN'T Run in GitHub Actions:

(Still runs on your Windows machine via Task Scheduler)

❌ Health monitor (every 4 hours)
❌ End-of-day learner (8 PM)

These only run on your Windows machine because they're Windows Task Scheduler jobs.

## Recommended Setup:

**GitHub Actions (Cloud):**
- 9 AM UTC = 4 AM ET (too early)
- Could use: 2:00 PM UTC = 9 AM ET ✓

**Windows Task Scheduler (Local):**
- Health monitor: Every 4 hours ✓
- End-of-day learner: 8 PM daily ✓

## To Check if Workflow is Running:

1. Go to Actions tab in your GitHub repo
2. Click "Daily MLB HR Prediction Pipeline"
3. Look at run history
4. Should see runs at 9 AM UTC each day (green checkmarks = success)

## Manual Trigger:

You can also run it manually without waiting for schedule:
1. Go to Actions tab
2. Click "Daily MLB HR Prediction Pipeline"
3. Click "Run workflow"
4. Select branch: main
5. Click "Run workflow"

## Current Status:

Workflow File: ✅ Created and properly configured
GitHub Actions: ⚠️ Needs verification (need secrets configured)
Windows Tasks: ✅ Set up (health monitor + end-of-day learner)

---

## OPTION: Change Workflow to 9 AM ET

If you want it to run at 9 AM ET (market open) instead of 9 AM UTC:

Change:
  cron: '0 9 * * *'

To:
  cron: '0 13 * * *'  (9 AM EDT = 1 PM UTC, or 13:00)

Or: 
  cron: '0 14 * * *'  (9 AM EST = 2 PM UTC, or 14:00)

GitHub Actions uses UTC only - no timezone offset support in cron.

---

## COMPLETE AUTOMATION SYSTEM:

GitHub Actions (Cloud - Daily):
  └─ 9 AM UTC (or configurable via cron)
     └─ Runs prediction pipeline
     └─ PHASE 0: Auto-learning from yesterday

Windows Task Scheduler (Local - 24/7):
  ├─ Every 4 hours: Health monitor (crash recovery)
  ├─ 8:00 PM: End-of-day learner (nightly analysis)
  └─ Auto on startup: Live monitor when predictions run

Combined = 24/7 fully automated system ✨

---

## To Fully Integrate:

Your current setup already has everything needed:

✅ GitHub Actions runs at 9 AM UTC (configure timezone as needed)
✅ Windows Tasks handle health monitoring
✅ Windows Tasks handle end-of-day learning
✅ Both systems feed into each other

No additional changes needed unless you want to:
1. Change the time from 9 AM UTC to 9 AM ET
2. Add end-of-day learner to GitHub Actions too
3. Make it manually triggerable
