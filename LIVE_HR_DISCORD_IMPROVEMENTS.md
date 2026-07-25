# Live HR Discord Feed Improvements

**Date**: 2026-07-24  
**Issue**: Live HRs were being sent asynchronously without confirmation of delivery

---

## Changes Made

### 1. **Synchronous Discord Sends (Critical)**
**Before**: `send_discord_webhook(..., async_send=True)`
- Spawned background thread
- Returned immediately without waiting
- Main loop didn't know if webhook succeeded

**After**: `send_discord_webhook(..., async_send=False, retries=3)`
- **Blocks until webhook confirms success**
- Only marks HR as processed after Discord confirms
- If webhook fails, HR stays in detection pool for next retry

**Impact**: No more lost HR alerts due to async fire-and-forget

---

### 2. **Retry Logic with Exponential Backoff**
- **3 retry attempts** for failed webhook sends
- **0.5s backoff** between retries
- Handles transient Discord API issues gracefully

**Scenario**: If Discord returns 429 (rate limit), we wait and retry instead of silently failing

---

### 3. **Discord Success Rate Tracking**
Added to live monitor status (`data/live_monitor_status.json`):
```json
{
  "detected_events_this_loop": 35,
  "sent_events_this_loop": 35,
  "discord_success_rate_pct": 100.0,
  "updated_at": "2026-07-24T23:19:42.507625"
}
```

**Metrics Now Visible**:
- `detected_events_this_loop` - HRs detected from MLB API
- `sent_events_this_loop` - HRs successfully sent to Discord
- `discord_success_rate_pct` - Success percentage (should be 100%)

---

### 4. **Webhook Validation Logging**
At startup, monitor now prints:
```
📡 Discord webhook: https://discord.com/api/webhooks/...
```
Helps verify the webhook is actually configured and valid

---

## Why This Matters

**Previous Behavior**:
- HR detected → Discord webhook spawned in background thread → Loop continues immediately
- If webhook fails, main loop has no idea
- HR marked as "processed" even if Discord never received it

**New Behavior**:
- HR detected → Synchronously wait for Discord confirmation (up to 3 retries)
- Only mark as "processed" when Discord returns success
- Monitor shows real-time success rate

---

## How to Verify

**Check Discord Success Rate**:
```bash
python -c "import json; print(json.load(open('data/live_monitor_status.json'))['discord_success_rate_pct'])"
```

Should show `100.0` or close to it.

**Monitor Live Delivery**:
```bash
tail -f data/live_monitor_status.json | python -m json.tool
```

Watch `discord_success_rate_pct` and `sent_events_this_loop` to confirm HRs are being sent.

---

## Testing

To test live HR Discord delivery:
1. Monitor will continue with next game day
2. Check `live_monitor_status.json` for success rate
3. Verify Discord channel receives alerts in real-time
4. Check `live_hr_processed_YYYY-MM-DD.json` for detected vs. sent count

---

**TL;DR**: HRs now sent **synchronously with retries** instead of async fire-and-forget. Delivery confirmation required before marking processed.
