import time

from run_daily_predictions import _compute_odds_request_wait_seconds


def test_compute_odds_request_wait_seconds_respects_window():
    now = time.time()
    history = [now - 10, now - 5]

    wait_seconds = _compute_odds_request_wait_seconds(history, max_per_minute=2, now_ts=now)
    assert wait_seconds > 0.0

    history = [now - 55, now - 30]
    wait_seconds = _compute_odds_request_wait_seconds(history, max_per_minute=2, now_ts=now)
    assert wait_seconds > 0.0
