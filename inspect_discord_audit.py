import argparse
import json
import re
from datetime import datetime
from pathlib import Path


def _load_rows(path: Path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    rows.sort(key=lambda r: str(r.get("timestamp", "")))
    return rows


def _extract_snapshot_counts(snapshot_text: str):
    def _grab(pattern: str):
        m = re.search(pattern, snapshot_text)
        return int(m.group(1)) if m else None

    return {
        "most_likely": _grab(r"Most Likely Homers:\s*(\d+)"),
        "radar": _grab(r"Delivered radar picks:\s*(\d+)"),
        "ev": _grab(r"Delivered \+EV picks:\s*(\d+)"),
    }


def main():
    parser = argparse.ArgumentParser(description="Inspect latest Discord audit snapshot and betting card")
    parser.add_argument("--date", help="Date in YYYY-MM-DD (defaults to today)")
    parser.add_argument("--preview", type=int, default=2200, help="Preview length for betting card content")
    args = parser.parse_args()

    day = args.date or datetime.today().strftime("%Y-%m-%d")
    audit_path = Path("data") / f"discord_alert_audit_{day}.jsonl"
    rows = _load_rows(audit_path)

    if not rows:
        print(f"No audit rows found in: {audit_path}")
        return

    snapshots = [r for r in rows if "MODEL SNAPSHOT" in str(r.get("title", ""))]
    cards = [r for r in rows if "MLB HR BETTING CARD" in str(r.get("title", ""))]

    print(f"Audit file: {audit_path}")
    print(f"Total rows: {len(rows)}")
    print(f"Snapshot messages: {len(snapshots)}")
    print(f"Betting card messages: {len(cards)}")

    if snapshots:
        latest_snapshot = snapshots[-1]
        snapshot_text = str(latest_snapshot.get("content", ""))
        counts = _extract_snapshot_counts(snapshot_text)
        print("\nLatest snapshot")
        print(f"Timestamp: {latest_snapshot.get('timestamp')}")
        print(snapshot_text)
        print(
            "Parsed counts:",
            f"most_likely={counts['most_likely']}",
            f"radar={counts['radar']}",
            f"ev={counts['ev']}",
        )

    if cards:
        latest_card = cards[-1]
        card_text = str(latest_card.get("content", ""))
        curated_2leg = card_text.count("• **")
        print("\nLatest betting card")
        print(f"Timestamp: {latest_card.get('timestamp')}")
        print(f"Has curated lotto section: {'LOTTO PARLAYS (CURATED)' in card_text}")
        print(f"Estimated bullet count: {curated_2leg}")
        print("Card preview:")
        print(card_text[: max(200, int(args.preview))])


if __name__ == "__main__":
    main()
