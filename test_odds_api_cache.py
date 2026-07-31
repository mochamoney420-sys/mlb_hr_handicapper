import json
from pathlib import Path

import run_daily_predictions as rdp


def test_load_cached_odds_payload(monkeypatch, tmp_path):
    cache_path = tmp_path / "odds_cache.json"
    payload = {
        "timestamp": "2026-07-31T12:00:00",
        "raw": {"Alec Bohm": {"draftkings": -150}},
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("ODDS_CACHE_FILE", str(cache_path))

    cached_payload, age_seconds = rdp._load_cached_hr_prop_odds_payload()

    assert cached_payload == payload["raw"]
    assert age_seconds is not None
    assert isinstance(age_seconds, (int, float))


def test_save_cached_odds_payload(tmp_path):
    cache_path = tmp_path / "odds_cache.json"
    data = {"Alec Bohm": {"draftkings": -150}}

    rdp._save_cached_hr_prop_odds_payload(data, cache_path=cache_path)

    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["raw"] == data
    assert "timestamp" in saved
