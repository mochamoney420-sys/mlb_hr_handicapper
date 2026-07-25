# Most Likely Homers Update

**Date**: 2026-07-24  
**Change**: Renamed "Top 5 Predictions" to "Most Likely Homers" with dynamic count

---

## What Changed

### 1. **Dynamic Count Instead of Fixed 5**
**Before**: 
- Always showed exactly top 5 predictions
- Limited visibility to only 5 candidates

**After**:
- Shows ALL predictions >= 6% confidence (configurable via `DISCORD_MIN_PROB`)
- Count changes based on how many homers model thinks will actually occur
- Today's display: **{count} Most Likely Homers**

### 2. **Terminology**
| Before | After |
|--------|-------|
| "Top 5 pick" | "Most Likely Homer" |
| "not a Top 5 list pick" | "not in Most Likely Homers" |
| "Top 5 Daily Projected HR Probabilities" | "Most Likely Homers (≥X% confidence) - N candidates" |
| `was_top5` field | `was_most_likely_homer` field |

### 3. **Configuration**
The "Most Likely Homers" threshold is:
- **Live HR alerts**: >= 12% probability (`most_likely_threshold = 0.12`)
  - When a live HR occurs, we check if it was in the model's "Most Likely Homers" list
- **Discord display**: >= 6% probability (configurable via `DISCORD_MIN_PROB`)
  - Shows all candidates model thinks have meaningful chance of hitting HR

---

## Example Output

**Before**:
```
Top 5 Daily Projected HR Probabilities:
| Shohei Ohtani    | Juan Soto        | Luis Robert Jr.  | Heriberto H.     | Yordan Alvarez   |
| 19.6%            | 17.7%            | 17.7%            | 17.4%            | 20.6%            |
```

**After**:
```
Most Likely Homers (≥6% confidence) - 47 candidates:
| Shohei Ohtani    | Sean Manaea      | 7:10 PM  | <= 2h      | 19.6%  | +117.8% | 0.0%   | 0.000  |
| Juan Soto        | Roki Sasaki      | 7:10 PM  | <= 2h      | 17.7%  | +97.0%  | 0.0%   | 0.000  |
| Luis Robert Jr.  | Roki Sasaki      | 7:10 PM  | <= 2h      | 17.7%  | +96.7%  | 0.0%   | 0.000  |
| ... (44 more candidates) |
```

---

## Discord Alerts

When a home run occurs:

**If HR was in Most Likely Homers (≥12%)**:
```
⚠️  LIVE HOME RUN ALERT ⚠️
🕐 Time: 2026-07-24 08:45:23 PM ET
🏟️ *Dodgers @ Mets* (top 2)
⚾ Solo home run.
👤 Batter: Luis Robert Jr.
🎯 Pitcher: Roki Sasaki
✅ Model called it! (Prob: 18.7%) — Most Likely Homer
```

**If HR was predicted but not in Most Likely Homers**:
```
⚠️  LIVE HOME RUN ALERT ⚠️
...
🎯 Pitcher: Eduardo Rodriguez
📊 Model rank: #47 (Prob: 8.3%) — not in Most Likely Homers
```

---

## CSV Fields Updated

`live_feedback_YYYY-MM-DD.csv` now includes:
- `was_most_likely_homer` (boolean)
  - `True` if HR batter was in ≥12% confidence list
  - `False` otherwise

---

## Benefits

1. **More Transparency**: Shows exactly how many homers model thinks will actually occur
2. **No Arbitrary Limits**: Not constrained to exactly 5 predictions
3. **Better Feedback**: Learning system knows which HRs were "expected" vs "lucky breaks"
4. **Scalable**: Works equally well on low-HR days (maybe 10 candidates) or high-scoring days (maybe 60+)

---

## Configuration

To adjust "Most Likely Homers" thresholds, set environment variables:

```bash
# Show all predictions >= 8% (default 6%)
DISCORD_MIN_PROB=0.08

# Show all predictions >= 12% (default 6%) 
DISCORD_MIN_PROB=0.12

# Adjust how many fit per Discord message (default 10)
DISCORD_ROWS_PER_MESSAGE=20
```

---

## Next Game Session

When monitor runs again:
1. Top-level output shows **all** candidates instead of just 5
2. Discord posts show **all** candidates instead of just 5
3. Live HR alerts compare against the full **Most Likely Homers** list
4. Learning feedback distinguishes between "called it" vs "close miss"

**TL;DR**: No more artificial "Top 5" limit. Shows all homers the model thinks will actually happen. ⚾
