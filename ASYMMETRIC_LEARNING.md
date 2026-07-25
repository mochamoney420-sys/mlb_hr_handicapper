# Asymmetric Learning System

## Overview

Transformed the learning system from **bidirectional** (equal treatment of right/wrong) to **asymmetric** (failures >> successes by 3-5x impact ratio).

## Philosophy

**Before (Bidirectional):** Model learns equally from what it got right and what it got wrong. Reinforces correct predictions.

**After (Asymmetric):** Model learns PRIMARILY from what it got WRONG. Failures get 3-10x more training signal than successes.

**Why:** 
- Model already knows to predict correctly (that was success)
- What the model NEEDS is to learn from mistakes
- False confidence is costly and must be penalized heavily
- Rare event prediction (HRs) requires aggressive negative feedback

## Learning Signals

### FAILURES (Aggressive Learning - 3-10x impact)
These get HEAVY upweighting/penalty to force the model to learn

| Signal | Condition | Weight | Purpose |
|--------|-----------|--------|---------|
| **Missed HR** | Pred < 0.10, HR happened | **+3.0x** | FIX: Model too conservative, BE MORE AGGRESSIVE |
| **Pitcher Miss** | Recent HR surge | **+1.5x** | FIX: Catch pitcher degradation |
| **False Pos (Batter)** | Pred > 0.25, no HR | **-0.5x** | FIX: Stop being so bullish |
| **False Pos (Pitcher)** | Pitcher FP pattern | **-0.4x** | FIX: Pitcher-specific false confidence |

### SUCCESSES (Minimal Learning - don't reinforce what works)
These get little/no upweighting since the model already does them correctly

| Signal | Condition | Weight | Purpose |
|--------|-----------|--------|---------|
| **Correct Skeptical** | Pred < 0.05, no HR | **+0.0x** | Model already does this, don't waste training signal |
| **Correct Confident** | Pred > 0.15, HR | **+0.3x** | Minimal reinforcement only |

## Boost Calculation

```python
boost = 1.0
        + (missed_hr × 3.0)              # FAILURES: Very aggressive on misses (3.0x vs 1.5x before)
        + (pit_missed × 1.5)             # FAILURES: Pitcher pattern (1.5x vs 0.6x before)
        - (false_pos_batter × 0.5)       # FAILURES: Strong penalty (0.5x vs 0.2x before)
        - (false_pos_pitcher × 0.4)      # FAILURES: Pitcher penalty (0.4x vs 0.15x before)
        + (correct_skeptical × 0.0)      # SUCCESS: Zero reinforcement (was 0.8x)
        + (correct_confident × 0.3)      # SUCCESS: Minimal reinforcement (was 1.2x)

Clipped: [0.5, 15.0]  (was [0.5, 10.0], increased range for aggressive failures)
```

## Impact Ratio

**Failures vs Successes:**
- 1 missed HR (3.0x) >> 10 correct confident predictions (10 × 0.3x = 3.0x total)
- 2 missed HRs (6.0x) >> 1 false positive (0.5x penalty)
- 1 pitcher miss pattern (1.5x) >> 5 correct skeptical predictions (0.0x = no impact)

**Key insight:** Failures drive 3-10x more learning than successes. Model forced to focus on what it got wrong, not what it got right.

## Diagnostic Output

```
✅ Asymmetric learning: 150 heavily upweighted (FAILURES), 45 penalized (FALSE CONFIDENCE)
```

Interpretation:
- 150 rows from missed HRs and pitcher degradation → model will be MORE AGGRESSIVE
- 45 rows with false confidence penalties → model will be LESS BULLISH
- Result: Better accuracy through failure-driven learning

## Expected Behavior

### Short Term (Days 1-3)
- Predictions become more aggressive (more HRs predicted)
- Fewer missed HRs as model learns from misses aggressively
- Some increase in false positives initially (trading off)

### Medium Term (Week 1+)
- Model finds optimal aggressive level
- False positive penalties kick in
- Balanced improvement: fewer misses, controlled false positives

### Long Term (Weeks+)
- Model learns to distinguish:
  - When to be aggressive (miss-prone pitchers, hot batters)
  - When to be cautious (false-confidence patterns)
- System self-corrects through asymmetric feedback

## Comparison: Before vs After

| Metric | Bidirectional | Asymmetric |
|--------|---------------|-----------|
| Missed HR boost | 1.5x | 3.0x |
| Correct prediction reinforcement | 1.2x | 0.3x |
| False positive penalty | -0.2x | -0.5x |
| Failures vs Successes impact ratio | 1:1 | 5:1 (asymmetric) |
| Max boost | 10.0x | 15.0x |
| Focus | Learn from both | Learn primarily from failures |

## Implementation Details

**File:** `run_daily_predictions.py`
**Function:** `load_feedback_weights()` (lines 2216+)

Changes:
1. Docstring updated to reflect asymmetric philosophy
2. Learning weights increased for failures (3.0x, 1.5x, -0.5x, -0.4x)
3. Learning weights decreased for successes (0.0x, 0.3x)
4. Diagnostic output now shows "heavily upweighted" vs "penalized"
5. Max boost increased from 10.0 to 15.0 to handle extreme cases

## Activation

Automatic on next `python run_daily_predictions.py` run:
- Model training uses asymmetric feedback weights
- Emphasis shifts to failure-driven learning
- HR detection accuracy should improve as misses are heavily penalized in training
