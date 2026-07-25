#!/usr/bin/env bash
# Quick validation that all improvements are in place

echo "=========================================="
echo "IMPROVEMENTS VALIDATION"
echo "=========================================="

echo ""
echo "1. Checking error_tracking.py exists..."
if [ -f "error_tracking.py" ]; then
    echo "   ✅ error_tracking.py created"
    lines=$(wc -l < error_tracking.py)
    echo "   ✅ Size: $lines lines"
else
    echo "   ❌ error_tracking.py NOT found"
    exit 1
fi

echo ""
echo "2. Checking error tracking import in run_daily_predictions.py..."
if grep -q "from error_tracking import" run_daily_predictions.py; then
    echo "   ✅ error_tracking import found"
else
    echo "   ❌ error_tracking import NOT found"
    exit 1
fi

echo ""
echo "3. Checking bidirectional learning..."
if grep -q "correct_confident" run_daily_predictions.py; then
    echo "   ✅ Bidirectional learning (correct_confident) found"
else
    echo "   ❌ correct_confident NOT found"
    exit 1
fi

if grep -q "correct_skeptical" run_daily_predictions.py; then
    echo "   ✅ Bidirectional learning (correct_skeptical) found"
else
    echo "   ❌ correct_skeptical NOT found"
    exit 1
fi

echo ""
echo "4. Checking health check scanner..."
if grep -q "SCANNING FOR SILENT FAILURES" run_daily_predictions.py; then
    echo "   ✅ Silent failure scanner found"
else
    echo "   ❌ Silent failure scanner NOT found"
    exit 1
fi

echo ""
echo "5. Checking error logging integration..."
if grep -q "log_error\|log_warning" run_daily_predictions.py; then
    count=$(grep -c "log_error\|log_warning" run_daily_predictions.py)
    echo "   ✅ Error logging calls: $count found"
else
    echo "   ❌ No error logging calls found"
    exit 1
fi

echo ""
echo "=========================================="
echo "✅ ALL VALIDATIONS PASSED"
echo "=========================================="
