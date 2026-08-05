"""Compliant free-source odds ingestion utilities.

This module intentionally avoids bypassing protected/private sportsbook APIs.
It only supports:
1) Public, unauthenticated pages explicitly provided by the user.
2) User-exported local HTML/CSV/JSON snapshots.

Use this as a fallback provider when direct API access is unavailable.
"""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_SHARP_PIPELINE_CACHE_TS = 0.0
_SHARP_PIPELINE_CACHE_DATA: Dict[str, Dict[str, int]] = {}


def _normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _safe_float(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(str(value).strip())
    except Exception:
        return None


def _extract_american_odds(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"([+-]\d{3,4})", text.replace("\u2212", "-"))
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _american_to_prob(odds: int) -> float:
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def _validate_row(player: str, book: str, odds: int) -> Optional[str]:
    """Return None when valid, else rejection reason."""
    p = (player or "").strip()
    b = (book or "").strip().lower()

    if len(p) < 3:
        return "player_name_too_short"
    if p.lower() in {"n/a", "unknown", "null"}:
        return "player_name_invalid"
    if not b:
        return "book_missing"
    if odds == 0:
        return "odds_zero_invalid"

    # Conservative American odds bounds for HR props.
    if odds > 0:
        if odds < 100 or odds > 5000:
            return "odds_out_of_range_positive"
    else:
        if odds > -100 or odds < -5000:
            return "odds_out_of_range_negative"

    return None


def _reject_log_path() -> Path:
    configured = os.getenv("FREE_ODDS_REJECT_LOG", "").strip()
    if configured:
        p = Path(configured)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    data_dir = Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / f"free_odds_rejects_{datetime.today().strftime('%Y-%m-%d')}.csv"


def _append_reject_rows(rows: List[Dict[str, str]]) -> None:
    if not rows:
        return
    path = _reject_log_path()
    exists = path.exists()
    fields = ["timestamp", "source", "player", "book", "odds", "reason"]
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _validate_and_filter(
    raw_map: Dict[str, Dict[str, int]],
    source_label: str,
) -> Dict[str, Dict[str, int]]:
    """Strictly validate odds rows and write reject logs for dropped records."""
    strict = os.getenv("FREE_ODDS_STRICT_SCHEMA", "true").strip().lower() != "false"
    if not strict:
        return raw_map

    cleaned: Dict[str, Dict[str, int]] = {}
    rejects: List[Dict[str, str]] = []
    ts = datetime.now().isoformat()

    for player, book_map in (raw_map or {}).items():
        for book, odds in (book_map or {}).items():
            try:
                odds_i = int(odds)
            except Exception:
                rejects.append(
                    {
                        "timestamp": ts,
                        "source": source_label,
                        "player": str(player),
                        "book": str(book),
                        "odds": str(odds),
                        "reason": "odds_not_integer",
                    }
                )
                continue

            reason = _validate_row(str(player), str(book), odds_i)
            if reason is not None:
                rejects.append(
                    {
                        "timestamp": ts,
                        "source": source_label,
                        "player": str(player),
                        "book": str(book),
                        "odds": str(odds_i),
                        "reason": reason,
                    }
                )
                continue

            cleaned.setdefault(str(player).strip(), {})[str(book).strip().lower()] = odds_i

    _append_reject_rows(rejects)
    return cleaned


def _parse_json_payload(payload: object) -> Dict[str, Dict[str, int]]:
    """Parse a flexible JSON schema into {player: {book: american_odds}}.

    Accepted shapes:
    - [{"player": "Name", "book": "dk", "odds": 550}, ...]
    - {"rows": [...same as above...]}
    - {"Name": {"draftkings": 550, "fanduel": 600}, ...}
    """
    out: Dict[str, Dict[str, int]] = {}

    def put(player: str, book: str, odds_val):
        player_key = player.strip()
        book_key = (book or "unknown").strip().lower()
        odds_f = _safe_float(odds_val)
        if not player_key or odds_f is None:
            return
        odds_i = int(round(odds_f))
        out.setdefault(player_key, {})[book_key] = odds_i

    if isinstance(payload, dict):
        if "rows" in payload and isinstance(payload["rows"], list):
            payload = payload["rows"]
        elif all(isinstance(v, dict) for v in payload.values()):
            for player, book_map in payload.items():
                for bk, o in book_map.items():
                    put(str(player), str(bk), o)
            return out

    if isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            player = row.get("player") or row.get("name") or row.get("batter")
            if not player:
                continue
            if "books" in row and isinstance(row["books"], dict):
                for bk, o in row["books"].items():
                    put(str(player), str(bk), o)
            else:
                book = row.get("book") or row.get("sportsbook") or "unknown"
                odds = row.get("odds") or row.get("price") or row.get("american")
                put(str(player), str(book), odds)

    return out


def load_odds_from_local_json(path: str) -> Dict[str, Dict[str, int]]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        parsed = _parse_json_payload(payload)
        return _validate_and_filter(parsed, f"local_json:{p.name}")
    except Exception:
        return {}


def load_odds_from_local_csv(path: str) -> Dict[str, Dict[str, int]]:
    """Expected columns: player, book, odds (or aliases)."""
    p = Path(path)
    if not p.exists():
        return {}

    out: Dict[str, Dict[str, int]] = {}
    try:
        with p.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                player = row.get("player") or row.get("name") or row.get("batter") or ""
                book = row.get("book") or row.get("sportsbook") or "unknown"
                odds_val = row.get("odds") or row.get("price") or row.get("american")
                odds_f = _safe_float(odds_val)
                if not player or odds_f is None:
                    continue
                out.setdefault(player.strip(), {})[book.strip().lower()] = int(round(odds_f))
    except Exception:
        return {}

    return _validate_and_filter(out, f"local_csv:{p.name}")


def parse_public_html_odds(html_text: str) -> Dict[str, Dict[str, int]]:
    """Generic table/list parser for public, unauthenticated pages.

    Heuristic approach:
    - Find table rows or card-like blocks containing a player name + american odds.
    - If sportsbook label is not present, defaults to book='public'.
    """
    if BeautifulSoup is None:
        return {}

    soup = BeautifulSoup(html_text, "html.parser")
    out: Dict[str, Dict[str, int]] = {}

    # Table-driven extraction
    for tr in soup.find_all("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if len(tds) < 2:
            continue

        odds = None
        for cell in tds:
            odds = _extract_american_odds(cell)
            if odds is not None:
                break
        if odds is None:
            continue

        # player guess: first non-empty cell not containing odds token
        player = ""
        for cell in tds:
            if _extract_american_odds(cell) is None and len(cell) > 2:
                player = cell
                break
        if not player:
            continue

        # book guess from row text
        row_text = " ".join(tds).lower()
        book = "public"
        if "draftkings" in row_text:
            book = "draftkings"
        elif "fanduel" in row_text:
            book = "fanduel"
        elif "betmgm" in row_text:
            book = "betmgm"

        out.setdefault(player.strip(), {})[book] = odds

    # Fallback: card text scan
    if not out:
        blocks = soup.find_all(["div", "li", "article"])
        for b in blocks:
            txt = b.get_text(" ", strip=True)
            odds = _extract_american_odds(txt)
            if odds is None:
                continue

            # crude name candidate: 2+ capitalized words before odds
            m = re.search(r"([A-Z][a-z]+\s+[A-Z][a-z'.-]+)", txt)
            if not m:
                continue
            player = m.group(1).strip()
            out.setdefault(player, {})["public"] = odds

    return out


def fetch_public_url_html(url: str, timeout: int = 10) -> str:
    if requests is None:
        return ""
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return ""
        return resp.text
    except Exception:
        return ""


def _fetch_public_json(url: str, timeout: int = 10) -> Optional[object]:
    """Attempt to fetch JSON from a public URL; return None on failure."""
    if requests is None:
        return None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def fetch_circa_sports_mlb() -> Optional[object]:
    """Public web gateway endpoint for Circa sports inventory."""
    url = os.getenv("FREE_ODDS_CIRCA_URL", "https://circasports.com").strip()
    return _fetch_public_json(url)


def fetch_sharp_aggregations() -> Optional[object]:
    """Public feed for sharp consensus pricing when available."""
    url = os.getenv("FREE_ODDS_SHARP_CONSENSUS_URL", "https://actionnetwork.com").strip()
    return _fetch_public_json(url)


def run_sharp_pipeline() -> Dict[str, Optional[object]]:
    """Fetch sharp source payloads for downstream parsing."""
    sharp_data = {
        "circa": fetch_circa_sports_mlb(),
        "sharp_consensus": fetch_sharp_aggregations(),
    }
    print("Sharp Pipeline Sync: Successfully pulled true market-maker lines.")
    return sharp_data


def _parse_sharp_pipeline_to_odds(sharp_data: Dict[str, Optional[object]]) -> Dict[str, Dict[str, int]]:
    """Parse sharp payloads into {player: {book: american_odds}} when possible."""
    merged: Dict[str, Dict[str, int]] = {}

    circa_parsed = _parse_json_payload(sharp_data.get("circa")) if sharp_data.get("circa") is not None else {}
    sharp_parsed = (
        _parse_json_payload(sharp_data.get("sharp_consensus"))
        if sharp_data.get("sharp_consensus") is not None
        else {}
    )

    for player, book_map in (circa_parsed or {}).items():
        merged.setdefault(player, {}).update(book_map)

    for player, book_map in (sharp_parsed or {}).items():
        merged.setdefault(player, {}).update(book_map)

    return merged


def load_odds_from_public_urls(urls: List[str]) -> Dict[str, Dict[str, int]]:
    merged: Dict[str, Dict[str, int]] = {}
    for url in urls:
        part: Dict[str, Dict[str, int]] = {}

        # Prefer JSON when a public endpoint returns machine-readable payloads.
        # Fallback to HTML parsing for public pages.
        if requests is not None:
            try:
                resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200:
                    content_type = (resp.headers.get("Content-Type", "") or "").lower()
                    if "json" in content_type or url.lower().endswith(".json"):
                        try:
                            payload = resp.json()
                            part = _parse_json_payload(payload)
                        except Exception:
                            part = {}
                    if not part:
                        part = parse_public_html_odds(resp.text)
            except Exception:
                part = {}

        if not part:
            html = fetch_public_url_html(url)
            if html:
                part = parse_public_html_odds(html)

        if not part:
            continue

        part = _validate_and_filter(part, f"public_url:{url}")
        for player, book_map in part.items():
            merged.setdefault(player, {}).update(book_map)
    return merged


def _discover_default_local_odds_files() -> Tuple[List[str], List[str]]:
    """Return default local JSON/CSV odds files to try if no env vars are provided."""
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)

    json_candidates = [
        data_dir / "hr_prop_odds.json",
        data_dir / "free_odds.json",
        data_dir / "odds.json",
        data_dir / "market_odds.json",
        data_dir / "hr_props.json",
        data_dir / "hr_prop_market.json",
        Path("hr_prop_odds.json"),
        Path("free_odds.json"),
        Path("odds.json"),
        Path("market_odds.json"),
    ]
    csv_candidates = [
        data_dir / "hr_prop_odds.csv",
        data_dir / "free_odds.csv",
        data_dir / "odds.csv",
        data_dir / "market_odds.csv",
        data_dir / "hr_props.csv",
        data_dir / "hr_prop_market.csv",
        Path("hr_prop_odds.csv"),
        Path("free_odds.csv"),
        Path("odds.csv"),
        Path("market_odds.csv"),
    ]

    seen = set()
    json_paths = []
    for path in json_candidates:
        if path and str(path) not in seen and path.exists():
            json_paths.append(str(path))
            seen.add(str(path))

    csv_paths = []
    for path in csv_candidates:
        if path and str(path) not in seen and path.exists():
            csv_paths.append(str(path))
            seen.add(str(path))
    return json_paths, csv_paths


def load_free_odds_sources() -> Dict[str, Dict[str, int]]:
    """Load odds from configured free/public sources.

    Environment options:
    - FREE_ODDS_JSON_PATH: local JSON export path
    - FREE_ODDS_CSV_PATH: local CSV export path
    - FREE_ODDS_PUBLIC_URLS: comma-separated public URLs (JSON feeds and/or HTML pages)

    If no explicit env values are set, the loader also checks common local files in the
    project data directory such as data/hr_prop_odds.json and data/hr_prop_odds.csv.
    """
    merged: Dict[str, Dict[str, int]] = {}

    json_path = os.getenv("FREE_ODDS_JSON_PATH", "").strip()
    csv_path = os.getenv("FREE_ODDS_CSV_PATH", "").strip()
    public_urls = [u.strip() for u in os.getenv("FREE_ODDS_PUBLIC_URLS", "").split(",") if u.strip()]

    if not json_path:
        default_json_paths, _ = _discover_default_local_odds_files()
        if default_json_paths:
            json_path = default_json_paths[0]

    if not csv_path:
        _, default_csv_paths = _discover_default_local_odds_files()
        if default_csv_paths:
            csv_path = default_csv_paths[0]

    if json_path:
        j = load_odds_from_local_json(json_path)
        for player, book_map in j.items():
            merged.setdefault(player, {}).update(book_map)

    if csv_path:
        c = load_odds_from_local_csv(csv_path)
        for player, book_map in c.items():
            merged.setdefault(player, {}).update(book_map)

    if public_urls:
        p = load_odds_from_public_urls(public_urls)
        for player, book_map in p.items():
            merged.setdefault(player, {}).update(book_map)

    use_sharp_pipeline = os.getenv("FREE_ODDS_USE_SHARP_PIPELINE", "false").strip().lower() in {"1", "true", "yes", "on"}
    if use_sharp_pipeline:
        try:
            import time

            global _SHARP_PIPELINE_CACHE_TS, _SHARP_PIPELINE_CACHE_DATA
            ttl_sec = max(30, int(float(os.getenv("FREE_ODDS_SHARP_TTL_SECONDS", "300"))))
            now_ts = time.time()

            if _SHARP_PIPELINE_CACHE_DATA and (now_ts - _SHARP_PIPELINE_CACHE_TS) < ttl_sec:
                sharp_rows = _SHARP_PIPELINE_CACHE_DATA
            else:
                sharp_payload = run_sharp_pipeline()
                sharp_rows = _parse_sharp_pipeline_to_odds(sharp_payload)
                sharp_rows = _validate_and_filter(sharp_rows, "sharp_pipeline") if sharp_rows else {}
                _SHARP_PIPELINE_CACHE_DATA = sharp_rows
                _SHARP_PIPELINE_CACHE_TS = now_ts

            for player, book_map in (sharp_rows or {}).items():
                merged.setdefault(player, {}).update(book_map)
        except Exception:
            pass

    return merged


def build_devigged_probs_from_books(raw_odds: Dict[str, Dict[str, int]]) -> Dict[str, float]:
    """Build a consensus probability map from raw odds by player name."""
    probs: Dict[str, float] = {}
    for player, book_map in raw_odds.items():
        implied = []
        for _, odds in (book_map or {}).items():
            try:
                implied.append(_american_to_prob(int(odds)) * 0.952)
            except Exception:
                continue
        if implied:
            probs[player] = round(sum(implied) / len(implied), 4)
    return probs
