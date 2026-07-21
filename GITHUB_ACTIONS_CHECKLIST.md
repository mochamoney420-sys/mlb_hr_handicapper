✅ GITHUB ACTIONS DAILY AUTOMATION - CHECKLIST

Your workflow is now configured to run at 9 AM ET (market open) daily.

========================================================================
VERIFICATION CHECKLIST
========================================================================

STEP 1: Set Up GitHub Secret
□ Go to: https://github.com/YOUR_USERNAME/mlb_hr_handicapper/settings/secrets/actions
□ Click "New repository secret"
□ Name: DISCORD_WEBHOOK_URL
□ Value: (paste your webhook from .vscode/.env)
□ Click "Add secret"

STEP 2: Verify Workflow File
□ Workflow updated to run at 9 AM ET (1 PM UTC)
□ File location: .github/workflows/daily_handicap.yml
□ Status: Ready to deploy

STEP 3: Check GitHub Actions is Enabled
□ Go to: https://github.com/YOUR_USERNAME/mlb_hr_handicapper
□ Click "Actions" tab
□ You should see "Daily MLB HR Prediction Pipeline"
□ If no jobs listed, it may not be enabled (usually auto-enabled)

STEP 4: Manual Test
□ Go to Actions tab
□ Click "Daily MLB HR Prediction Pipeline"
□ Click "Run workflow"
□ Select "main" branch
□ Click "Run workflow"
□ Wait 2-5 minutes for completion
□ Check Discord for predictions
□ Verify CSV artifact uploaded

STEP 5: Verify Scheduled Run
□ Wait for next 9 AM ET
□ Check Actions tab for run history
□ Should show green checkmark = success
□ Verify Discord received predictions

========================================================================
WHAT RUNS WHEN
========================================================================

GitHub Actions (Cloud - Automated):
  └─ 9:00 AM ET (1 PM UTC) daily
     ├─ Check out repository
     ├─ Setup Python 3.10
     ├─ Install requirements
     ├─ Run: python run_daily_predictions.py
     │  ├─ PHASE 0: Analyze yesterday's HRs (auto-learning)
     │  ├─ PHASE 1: Load training data
     │  ├─ PHASE 2: Train ensemble
     │  ├─ PHASE 3: Generate predictions
     │  └─ PHASE 4: Spawn live monitor
     ├─ Send predictions to Discord
     └─ Upload predictions CSV as artifact

Windows Task Scheduler (Local - Continuous):
  ├─ Every 4 hours: Health monitor (auto-fixes crashes)
  └─ 8 PM daily: End-of-day learner (nightly analysis)

Combined = 24/7 Fully Automated System ✨

========================================================================
HOW TO MONITOR
========================================================================

Check GitHub Actions Run History:
  1. Go to https://github.com/YOUR_USERNAME/mlb_hr_handicapper
  2. Click "Actions" tab
  3. Click "Daily MLB HR Prediction Pipeline"
  4. See all scheduled runs with status
  5. Click any run to see detailed logs

Check Workflow Logs:
  1. From Actions tab, click a run
  2. Click "run-pipeline" job
  3. Expand steps to see output:
     - "Set up Python environment" logs
     - "Install dependencies" logs
     - "Execute handicapping model script" output
     - "Upload predictions CSV" confirmation

View Uploaded Predictions:
  1. From Actions tab, click a run
  2. Scroll down to "Artifacts"
  3. Click "daily-predictions" zip
  4. Contains: predictions_YYYY-MM-DD.csv

========================================================================
TIMEZONE NOTE
========================================================================

GitHub Actions uses UTC only (no timezone offset support).

Conversion:
  ✅ 9 AM ET (Eastern) = 1 PM UTC = cron '0 13 * * *'
  ✅ 9 AM CT (Central) = 2 PM UTC = cron '0 14 * * *'
  ✅ 9 AM PT (Pacific) = 4 PM UTC = cron '0 16 * * *'

Current setting: cron '0 13 * * *' = 9 AM ET ✓

If you need different timezone, update the workflow:
  Line 5: cron: '0 XX * * *'  (where XX = desired UTC hour)

========================================================================
TROUBLESHOOTING
========================================================================

Workflow not running at 9 AM?
  1. Check "Actions" tab enabled (usually is by default)
  2. Verify DISCORD_WEBHOOK_URL secret is set
  3. Check workflow file syntax (should be valid YAML)
  4. Try manual trigger first to verify setup works

Discord not receiving predictions?
  1. Check DISCORD_WEBHOOK_URL secret is correct
  2. Manual test: python run_daily_predictions.py locally
  3. Verify webhook URL still valid (webhooks can expire)

Predictions CSV not uploading?
  1. Check that predictions generation succeeded (see logs)
  2. Verify data/predictions_*.csv file exists
  3. Check artifact upload step in logs

Python dependencies failing?
  1. requirements.txt must exist in repo root
  2. All packages must be pip-installable
  3. Python 3.10 should have everything needed

========================================================================
YOUR COMPLETE AUTOMATION
========================================================================

You now have a FULLY AUTOMATED MLB HR prediction system:

✅ GitHub Actions (Cloud):
   - Runs at 9 AM ET daily
   - Executes prediction pipeline
   - Includes auto-learning from yesterday
   - Sends to Discord
   - Backs up predictions

✅ Windows Task Scheduler (Local):
   - Health monitor: Every 4 hours (24/7)
   - End-of-day learner: 8 PM daily
   - Live monitoring: Auto-spawned throughout day

✅ Local Development:
   - Can run manually: python run_daily_predictions.py
   - Can check health: python health_monitor.py
   - Can view learning: python analyze_hr_patterns.py

Combined = Professional sports analytics system running 24/7 ✨

========================================================================
NEXT STEPS
========================================================================

Immediate:
  1. Set DISCORD_WEBHOOK_URL secret in GitHub
  2. Test with manual run
  3. Wait for 9 AM ET tomorrow to see automatic run

Verification:
  1. Check Actions tab for successful runs
  2. Verify Discord receives daily predictions
  3. Review uploaded prediction CSVs

Monitoring:
  1. Visit Actions tab daily to check status
  2. Review learning reports: data/hr_learning_report_*.json
  3. Track model performance over time

========================================================================
You're all set! System is fully automated. 🚀
========================================================================
