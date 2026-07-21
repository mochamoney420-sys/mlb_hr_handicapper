"""Plate-appearance physics and context simulation pipeline.

This module augments baseline model outputs with:
- Hourly live environment and density altitude
- Roof-aware 3D wind vectors vs stadium outfield geometry
- Pitch-level batter/pitcher micro-matchups
- Attack-angle vs VAA interaction
- Umpire + catcher framing cascade
- Biomechanical fatigue and lineup protection context
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
import statsapi

try:
    import requests
except Exception:  # pragma: no cover - fallback only used when requests missing
    requests = None

from src.physics_ballistics import DensityAltitudeCalculator
from src.stadium_coordinates import STADIUM_COORDINATES

TEAM_ABBR_TO_STADIUM_KEY = {
    "NYY": "Yankees",
    "BOS": "Red Sox",
    "BAL": "Orioles",
    "TB": "Rays",
    "TOR": "Blue Jays",
    "DET": "Tigers",
    "CWS": "White Sox",
    "KC": "Royals",
    "MIN": "Twins",
    "HOU": "Astros",
    "SEA": "Mariners",
    "TEX": "Rangers",
    "OAK": "Athletics",
    "NYM": "Mets",
    "ATL": "Braves",
    "PHI": "Phillies",
    "MIA": "Marlins",
    "WSH": "Nationals",
    "CHC": "Cubs",
    "CIN": "Reds",
    "MIL": "Brewers",
    "PIT": "Pirates",
    "STL": "Cardinals",
    "LAD": "Dodgers",
    "SD": "Padres",
    "SF": "Giants",
    "COL": "Rockies",
    "ARI": "Diamondbacks",
    "LAA": "Angels",
    "CLE": "Guardians",
}


def _safe_float(value, default=0.0) -> float:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return default
        return float(value)
    except Exception:
        return default


def _bounded(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _safe_int(value, default=0) -> int:
    try:
        if value is None:
            return int(default)
        if isinstance(value, float) and math.isnan(value):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _http_get_json(url: str, timeout: int = 7) -> dict:
    if requests is not None:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200:
            return {}
        return resp.json()
    return {}


def _fetch_weather_underground(lat: float, lon: float) -> Dict[str, float]:
    """Fetch localized hourly weather from Weather Underground/Weather.com API.

    Requires WEATHER_UNDERGROUND_API_KEY in environment.
    Returns empty dict when unavailable.
    """
    import os

    api_key = os.getenv("WEATHER_UNDERGROUND_API_KEY")
    if not api_key:
        return {}

    try:
        url = (
            "https://api.weather.com/v3/wx/forecast/hourly/2day"
            f"?geocode={lat},{lon}&format=json&units=e&language=en-US&apiKey={api_key}"
        )
        payload = _http_get_json(url, timeout=8)
        if not payload:
            return {}

        # Use first available forecast hour as proxy for near-game conditions.
        temp = _safe_float(payload.get("temperature", [None])[0], None)
        humidity = _safe_float(payload.get("relativeHumidity", [None])[0], None)
        wind_speed = _safe_float(payload.get("windSpeed", [None])[0], None)
        wind_dir = _safe_float(payload.get("windDirection", [None])[0], None)
        pressure_inhg = _safe_float(payload.get("pressureAltimeter", [None])[0], None)

        out = {}
        if temp is not None:
            out["temperature_f"] = temp
        if humidity is not None:
            out["humidity_pct"] = humidity
        if wind_speed is not None:
            out["wind_speed_mph"] = wind_speed
        if wind_dir is not None:
            out["wind_direction_deg"] = wind_dir
        if pressure_inhg is not None:
            out["pressure_inhg"] = pressure_inhg
        return out
    except Exception:
        return {}


def _parse_roof_status(game_data: dict, default_status: str = "open") -> str:
    weather = game_data.get("gameData", {}).get("weather", {})
    for key in ("condition", "conditions", "roof", "roofType"):
        text = str(weather.get(key, "")).lower()
        if "closed" in text:
            return "closed"
        if "retract" in text:
            return "retracted"
        if "open" in text:
            return "open"
    return default_status


def fetch_hourly_environment(game_pk: int, home_team_abbr: str, venue_id: Optional[int] = None) -> Dict[str, float]:
    """Fetch environment for game hour and compute DA + air density.

    Returns weather and atmospheric metrics with safe defaults when APIs fail.
    """
    stadium_key = TEAM_ABBR_TO_STADIUM_KEY.get(str(home_team_abbr), "")
    stadium = STADIUM_COORDINATES.get(stadium_key, {})

    lat = _safe_float(stadium.get("latitude"), 39.5)
    lon = _safe_float(stadium.get("longitude"), -98.35)
    altitude_ft = int(_safe_float(stadium.get("elevation_ft"), 500.0))
    default_roof = str(stadium.get("roof_status", "open"))

    roof_status = default_roof
    game_start = datetime.utcnow()

    try:
        game_data = statsapi.get("game", {"gamePk": game_pk})
        roof_status = _parse_roof_status(game_data, default_roof)

        venue = game_data.get("gameData", {}).get("venue", {})
        loc = venue.get("location", {})
        lat = _safe_float(loc.get("latitude"), lat)
        lon = _safe_float(loc.get("longitude"), lon)

        dt_raw = game_data.get("gameData", {}).get("datetime", {}).get("dateTime", "")
        if dt_raw:
            game_start = datetime.fromisoformat(dt_raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass

    temperature_f = 70.0
    humidity_pct = 50.0
    pressure_inhg = 29.92
    wind_speed_mph = 5.0
    wind_direction_deg = 0.0

    try:
        wu = _fetch_weather_underground(lat, lon)
        if wu:
            temperature_f = _safe_float(wu.get("temperature_f"), temperature_f)
            humidity_pct = _safe_float(wu.get("humidity_pct"), humidity_pct)
            pressure_inhg = _safe_float(wu.get("pressure_inhg"), pressure_inhg)
            wind_speed_mph = _safe_float(wu.get("wind_speed_mph"), wind_speed_mph)
            wind_direction_deg = _safe_float(wu.get("wind_direction_deg"), wind_direction_deg)

    except Exception:
        pass

    try:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m"
            "&temperature_unit=fahrenheit&windspeed_unit=mph"
        )
        payload = _http_get_json(url, timeout=7)
        hourly = payload.get("hourly", {})
        times = hourly.get("time", [])
        if times:
            target_hour = game_start.replace(minute=0, second=0, microsecond=0)
            idx = min(
                range(len(times)),
                key=lambda i: abs(datetime.fromisoformat(times[i]) - target_hour),
            )
            temperature_f = _safe_float(hourly.get("temperature_2m", [temperature_f])[idx], temperature_f)
            humidity_pct = _safe_float(hourly.get("relative_humidity_2m", [humidity_pct])[idx], humidity_pct)
            pressure_hpa = _safe_float(hourly.get("surface_pressure", [1013.25])[idx], 1013.25)
            pressure_inhg = pressure_hpa * 0.0295299831
            wind_speed_mph = _safe_float(hourly.get("wind_speed_10m", [wind_speed_mph])[idx], wind_speed_mph)
            wind_direction_deg = _safe_float(hourly.get("wind_direction_10m", [wind_direction_deg])[idx], wind_direction_deg)
    except Exception:
        pass

    # Indoors/retracted roofs dampen true wind substantially.
    if roof_status == "closed":
        wind_speed_mph = 0.5
    elif roof_status == "retracted":
        wind_speed_mph *= 0.65

    da_ft = DensityAltitudeCalculator.calculate_density_altitude(
        temp_f=temperature_f,
        pressure_inHg=pressure_inhg,
        altitude_ft=altitude_ft,
        humidity_pct=humidity_pct,
    )
    air_density = DensityAltitudeCalculator.get_air_density(
        temp_f=temperature_f,
        pressure_inHg=pressure_inhg,
        humidity_pct=humidity_pct,
        altitude_ft=altitude_ft,
    )

    drag_multiplier = _bounded(1.225 / max(air_density, 0.7), 0.82, 1.25)

    return {
        "temperature_f": temperature_f,
        "humidity_pct": humidity_pct,
        "pressure_inhg": pressure_inhg,
        "altitude_ft": altitude_ft,
        "roof_status": roof_status,
        "wind_speed_mph": wind_speed_mph,
        "wind_direction_deg": wind_direction_deg,
        "density_altitude_ft": da_ft,
        "air_density_kg_m3": air_density,
        "drag_multiplier": drag_multiplier,
    }


def compute_outfield_wind_vectors(home_team_abbr: str, wind_speed_mph: float, wind_direction_deg: float) -> Dict[str, float]:
    """Project wind onto RF/CF/LF axes using stadium orientation and wall geometry."""
    stadium_key = TEAM_ABBR_TO_STADIUM_KEY.get(str(home_team_abbr), "")
    stadium = STADIUM_COORDINATES.get(stadium_key, {})
    orientation = _safe_float(stadium.get("orientation_deg"), 0.0)
    geometry = stadium.get("outfield_geometry", {})

    # Absolute azimuths for outfield walls from home plate.
    target_azimuth = {
        "rf": (orientation + 45) % 360,
        "cf": orientation % 360,
        "lf": (orientation - 45) % 360,
    }

    def comp(target_deg: float) -> float:
        angle = math.radians((target_deg - wind_direction_deg) % 360)
        return wind_speed_mph * math.cos(angle)

    rf_component = comp(target_azimuth["rf"])
    cf_component = comp(target_azimuth["cf"])
    lf_component = comp(target_azimuth["lf"])

    # Scale by wall distance so same wind affects deeper alleys less per foot.
    rf_dist = _safe_float(geometry.get("rf_distance_ft"), 330.0)
    cf_dist = _safe_float(geometry.get("cf_distance_ft"), 400.0)
    lf_dist = _safe_float(geometry.get("lf_distance_ft"), 330.0)

    rf_mult = _bounded(1.0 + (rf_component / max(rf_dist, 250.0)) * 6.5, 0.82, 1.20)
    cf_mult = _bounded(1.0 + (cf_component / max(cf_dist, 300.0)) * 6.0, 0.82, 1.18)
    lf_mult = _bounded(1.0 + (lf_component / max(lf_dist, 250.0)) * 6.5, 0.82, 1.20)

    return {
        "wind_out_component_rf": rf_component,
        "wind_out_component_cf": cf_component,
        "wind_out_component_lf": lf_component,
        "wind_vector_multiplier_rf": rf_mult,
        "wind_vector_multiplier_cf": cf_mult,
        "wind_vector_multiplier_lf": lf_mult,
    }


def _estimate_pitch_vaa_deg(pitch_df: pd.DataFrame) -> float:
    if "vaa" in pitch_df.columns:
        v = pd.to_numeric(pitch_df["vaa"], errors="coerce").dropna()
        if not v.empty:
            return float(v.mean())

    rel_z = pd.to_numeric(pitch_df.get("release_pos_z", pd.Series(dtype=float)), errors="coerce")
    plate_z = pd.to_numeric(pitch_df.get("plate_z", pd.Series(dtype=float)), errors="coerce")
    ext = pd.to_numeric(pitch_df.get("release_extension", pd.Series(dtype=float)), errors="coerce").fillna(6.0)
    valid = rel_z.notna() & plate_z.notna()
    if not valid.any():
        return -5.5

    travel_ft = (60.5 - ext[valid]).clip(lower=45.0)
    slope = (plate_z[valid] - rel_z[valid]) / travel_ft
    vaa = np.degrees(np.arctan(slope))
    return float(np.nanmean(vaa)) if len(vaa) else -5.5


def compute_pitch_level_micro_matchup(
    batter_id: int,
    pitcher_id: int,
    batter_hand: str,
    statcast_df: pd.DataFrame,
) -> Dict[str, float]:
    """Pitch-level arsenal and VAA/attack-angle interaction scores."""
    if statcast_df is None or statcast_df.empty:
        return {
            "pitch_micro_matchup_score": 1.0,
            "vaa_attack_angle_score": 1.0,
            "estimated_pitcher_vaa": -5.5,
            "estimated_batter_attack_angle": 12.0,
        }

    batter_df = statcast_df[pd.to_numeric(statcast_df.get("batter"), errors="coerce") == int(batter_id)].copy()
    pitcher_df = statcast_df[pd.to_numeric(statcast_df.get("pitcher"), errors="coerce") == int(pitcher_id)].copy()

    matchup_df = statcast_df[
        (pd.to_numeric(statcast_df.get("batter"), errors="coerce") == int(batter_id))
        & (pd.to_numeric(statcast_df.get("pitcher"), errors="coerce") == int(pitcher_id))
    ].copy()

    if matchup_df.empty:
        matchup_df = pitcher_df.copy()

    swings = batter_df[pd.to_numeric(batter_df.get("launch_angle"), errors="coerce").notna()].copy()
    attack_angle = float(pd.to_numeric(swings.get("launch_angle"), errors="coerce").median()) if not swings.empty else 12.0

    pitch_vaa = _estimate_pitch_vaa_deg(pitcher_df)

    # Pitch micro-splits by pitch type with velocity/spin tiering.
    matchup_df["release_speed"] = pd.to_numeric(matchup_df.get("release_speed"), errors="coerce")
    matchup_df["release_spin_rate"] = pd.to_numeric(matchup_df.get("release_spin_rate"), errors="coerce")
    matchup_df["launch_speed"] = pd.to_numeric(matchup_df.get("launch_speed"), errors="coerce")

    if matchup_df.empty:
        micro_score = 1.0
    else:
        pitch_group = matchup_df.groupby("pitch_type", dropna=False).agg(
            n=("pitch_type", "size"),
            ev=("launch_speed", "mean"),
            velo=("release_speed", "mean"),
            spin=("release_spin_rate", "mean"),
            hr=("events", lambda x: (x == "home_run").mean()),
            barrel=("launch_speed", lambda x: (pd.to_numeric(x, errors="coerce") >= 98).mean()),
        )

        if pitch_group.empty:
            micro_score = 1.0
        else:
            # Heavier weight to high-frequency arsenal.
            weights = pitch_group["n"] / max(pitch_group["n"].sum(), 1)
            velo_tier = (pitch_group["velo"] - pitch_group["velo"].mean()).fillna(0) / 8.0
            spin_tier = (pitch_group["spin"] - pitch_group["spin"].mean()).fillna(0) / 600.0
            damage = (pitch_group["hr"].fillna(0) * 4.0 + pitch_group["barrel"].fillna(0) * 2.2 + pitch_group["ev"].fillna(87.0) / 120.0)
            # Lower velo/spin consistency can increase damage risk if execution quality drops.
            vulnerability = damage * (1.0 - 0.08 * velo_tier - 0.05 * spin_tier)
            micro_score = float((vulnerability * weights).sum())
            micro_score = _bounded(0.85 + micro_score * 0.18, 0.75, 1.35)

    # High/flat VAA + steep attack angle interaction.
    # Pitch VAA is negative. A flatter pitch is closer to 0.
    vaa_flatness = _bounded((-4.0 - pitch_vaa) / 4.0, -0.5, 1.0)
    steep_swing = _bounded((attack_angle - 12.0) / 10.0, -0.5, 1.0)
    vaa_attack_score = _bounded(1.0 + (vaa_flatness * steep_swing * 0.35), 0.80, 1.30)

    return {
        "pitch_micro_matchup_score": micro_score,
        "vaa_attack_angle_score": vaa_attack_score,
        "estimated_pitcher_vaa": pitch_vaa,
        "estimated_batter_attack_angle": attack_angle,
    }


def compute_umpire_catcher_cascade(game_pk: int, pitcher_id: int, statcast_df: pd.DataFrame) -> Dict[str, float]:
    """Estimate strike-zone pressure from umpire profile plus catcher framing tendency."""
    umpire_impact = 1.0
    catcher_framing_impact = 1.0

    try:
        g = statsapi.get("game", {"gamePk": game_pk})
        officials = g.get("liveData", {}).get("boxscore", {}).get("officials", [])
        hp_name = ""
        if officials:
            hp_name = officials[0].get("person", {}).get("fullName", "")

        # A larger zone hurts hitter outcomes.
        lower = hp_name.lower()
        if any(name in lower for name in ("bucknor", "bellino", "hernandez")):
            umpire_impact = 0.94
        elif any(name in lower for name in ("layne", "lee")):
            umpire_impact = 1.06
    except Exception:
        pass

    if statcast_df is not None and not statcast_df.empty and "catcher" in statcast_df.columns:
        try:
            p_df = statcast_df[pd.to_numeric(statcast_df.get("pitcher"), errors="coerce") == int(pitcher_id)].copy()
            if not p_df.empty and "description" in p_df.columns:
                # Framing proxy: called strike share on borderline takes.
                called = p_df[p_df["description"].isin(["called_strike", "ball"])]
                if not called.empty:
                    called_rate = (called["description"] == "called_strike").mean()
                    catcher_framing_impact = _bounded(1.0 - ((called_rate - 0.33) * 0.35), 0.90, 1.08)
        except Exception:
            pass

    return {
        "umpire_strike_zone_multiplier": umpire_impact,
        "catcher_framing_multiplier": catcher_framing_impact,
        "umpire_catcher_cascade": _bounded(umpire_impact * catcher_framing_impact, 0.85, 1.15),
    }


def compute_fatigue_and_lineup_protection(
    batter_id: int,
    batting_order_slot: int,
    live_team_df: pd.DataFrame,
    statcast_df: pd.DataFrame,
) -> Dict[str, float]:
    """Estimate biomechanical fatigue and lineup protection from recent usage."""
    fatigue_index = 0.0
    fatigue_multiplier = 1.0

    if statcast_df is not None and not statcast_df.empty:
        try:
            b_df = statcast_df[pd.to_numeric(statcast_df.get("batter"), errors="coerce") == int(batter_id)].copy()
            if not b_df.empty:
                dates = pd.to_datetime(b_df.get("game_date"), errors="coerce").dropna().sort_values()
                if not dates.empty:
                    last14 = dates[dates >= (pd.Timestamp.now() - pd.Timedelta(days=14))]
                    games_14d = int(last14.dt.date.nunique())

                    # Consecutive-day streak.
                    unique_days = sorted(last14.dt.date.unique())
                    streak = 1
                    for i in range(len(unique_days) - 1, 0, -1):
                        if (unique_days[i] - unique_days[i - 1]).days == 1:
                            streak += 1
                        else:
                            break

                    # High leverage proxy from late innings with men on.
                    inning = pd.to_numeric(b_df.get("inning"), errors="coerce").fillna(0)
                    leverage_like = ((inning >= 7) & (b_df.get("on_2b", pd.Series(False)).notna())).mean()

                    fatigue_index = _bounded((games_14d * 2.3) + (streak * 3.5) + (float(leverage_like) * 20), 0, 100)
                    fatigue_multiplier = _bounded(1.06 - (fatigue_index / 220.0), 0.82, 1.06)
        except Exception:
            pass

    # Lineup protection from hitters immediately behind this batter.
    lineup_multiplier = 1.0
    try:
        if live_team_df is not None and not live_team_df.empty:
            slot = int(batting_order_slot) if pd.notna(batting_order_slot) else 5
            behind = live_team_df[live_team_df["batting_order_slot"].isin([slot + 1, slot + 2])].copy()
            if not behind.empty:
                hr_rate = pd.to_numeric(behind.get("bat_hr_rate"), errors="coerce").fillna(0.03)
                iso = pd.to_numeric(behind.get("bat_iso_proxy"), errors="coerce").fillna(0.08)
                # Practical wOBA proxy from available columns.
                woba_proxy = (0.70 * hr_rate) + (0.30 * iso)
                avg_woba = float(woba_proxy.mean())
                lineup_multiplier = _bounded(0.92 + (avg_woba * 2.2), 0.88, 1.14)
            else:
                avg_woba = 0.105
    except Exception:
        avg_woba = 0.105

    return {
        "fatigue_index": fatigue_index,
        "fatigue_multiplier": fatigue_multiplier,
        "lineup_protection_woba_proxy": avg_woba,
        "lineup_protection_multiplier": lineup_multiplier,
    }


def compute_pitcher_spin_decay(pitcher_id: int, statcast_df: pd.DataFrame) -> Dict[str, float]:
    """Compute four-seam spin decay over recent starts.

    Flags fatigue-like decay when recent spin drops by >50 RPM.
    """
    if statcast_df is None or statcast_df.empty:
        return {
            "spin_decay_rpm": 0.0,
            "spin_decay_multiplier": 1.0,
            "spin_decay_flag": 0,
        }

    try:
        pdf = statcast_df[pd.to_numeric(statcast_df.get("pitcher"), errors="coerce") == int(pitcher_id)].copy()
        if pdf.empty:
            raise ValueError("empty pitcher sample")

        pdf = pdf[pdf.get("pitch_type").isin(["FF", "FA", "SI", "FT"])].copy()
        pdf["release_spin_rate"] = pd.to_numeric(pdf.get("release_spin_rate"), errors="coerce")
        pdf["game_date"] = pd.to_datetime(pdf.get("game_date"), errors="coerce")
        pdf = pdf.dropna(subset=["release_spin_rate", "game_date"])
        if pdf.empty:
            raise ValueError("empty spin sample")

        by_game = pdf.groupby(pdf["game_date"].dt.date)["release_spin_rate"].mean().reset_index()
        by_game = by_game.sort_values("game_date")

        recent = by_game.tail(3)["release_spin_rate"].mean()
        baseline = by_game.head(max(len(by_game) - 3, 1))["release_spin_rate"].mean()
        decay = float(baseline - recent)

        flag = 1 if decay >= 50 else 0
        mult = _bounded(1.0 + (max(decay, 0.0) / 500.0), 1.0, 1.25)
        return {
            "spin_decay_rpm": decay,
            "spin_decay_multiplier": mult,
            "spin_decay_flag": flag,
        }
    except Exception:
        return {
            "spin_decay_rpm": 0.0,
            "spin_decay_multiplier": 1.0,
            "spin_decay_flag": 0,
        }


def simulate_plate_appearance_probability(
    row: pd.Series,
    statcast_df: pd.DataFrame,
    projected_pas: float,
    live_team_df: pd.DataFrame,
    env_cache: Dict[int, Dict[str, float]],
    num_simulations: int = 10000,
) -> Dict[str, float]:
    """Simulate per-PA HR and convert to game-level HR probability."""
    game_pk = _safe_int(row.get("game_pk", 0), 0)
    if game_pk not in env_cache:
        env_cache[game_pk] = fetch_hourly_environment(
            game_pk=game_pk,
            home_team_abbr=str(row.get("home_team", "")),
            venue_id=_safe_int(row.get("venue_id", 0), 0),
        )
    env = env_cache[game_pk]

    wind_map = compute_outfield_wind_vectors(
        home_team_abbr=str(row.get("home_team", "")),
        wind_speed_mph=_safe_float(env.get("wind_speed_mph"), 5.0),
        wind_direction_deg=_safe_float(env.get("wind_direction_deg"), 0.0),
    )

    micro = compute_pitch_level_micro_matchup(
        batter_id=_safe_int(row.get("batter", 0), 0),
        pitcher_id=_safe_int(row.get("pitcher", 0), 0),
        batter_hand=str(row.get("batter_hand", "R") or "R"),
        statcast_df=statcast_df,
    )

    ump_catch = compute_umpire_catcher_cascade(
        game_pk=game_pk,
        pitcher_id=_safe_int(row.get("pitcher", 0), 0),
        statcast_df=statcast_df,
    )

    fatigue = compute_fatigue_and_lineup_protection(
        batter_id=_safe_int(row.get("batter", 0), 0),
        batting_order_slot=_safe_int(row.get("batting_order_slot", 5), 5),
        live_team_df=live_team_df,
        statcast_df=statcast_df,
    )

    spin_decay = compute_pitcher_spin_decay(
        pitcher_id=_safe_int(row.get("pitcher", 0), 0),
        statcast_df=statcast_df,
    )

    base_hr_rate = _safe_float(row.get("bat_hr_rate"), 0.03)
    base_pitch_hr_allowed = _safe_float(row.get("pitch_hr_allowed_rate"), 0.03)

    # Blend hitter and pitcher priors into per-PA base rate.
    base_per_pa = _bounded((0.58 * base_hr_rate) + (0.42 * base_pitch_hr_allowed), 0.005, 0.22)

    # Use max of pull-side wind vectors as the likely target boost.
    wind_boost = max(
        _safe_float(wind_map.get("wind_vector_multiplier_rf"), 1.0),
        _safe_float(wind_map.get("wind_vector_multiplier_lf"), 1.0),
        _safe_float(wind_map.get("wind_vector_multiplier_cf"), 1.0),
    )

    context_mult = (
        _safe_float(env.get("drag_multiplier"), 1.0)
        * wind_boost
        * _safe_float(micro.get("pitch_micro_matchup_score"), 1.0)
        * _safe_float(micro.get("vaa_attack_angle_score"), 1.0)
        * _safe_float(ump_catch.get("umpire_catcher_cascade"), 1.0)
        * _safe_float(fatigue.get("fatigue_multiplier"), 1.0)
        * _safe_float(fatigue.get("lineup_protection_multiplier"), 1.0)
        * _safe_float(spin_decay.get("spin_decay_multiplier"), 1.0)
    )

    per_pa_prob = _bounded(base_per_pa * context_mult, 0.003, 0.35)

    # Monte Carlo at plate-appearance granularity.
    pa_draws = np.maximum(np.random.normal(projected_pas, 0.45, num_simulations), 1.0).astype(int)
    no_hr_prob = (1 - per_pa_prob) ** pa_draws
    game_hr_prob = float(1.0 - no_hr_prob.mean())

    return {
        "physics_per_pa_hr_prob": per_pa_prob,
        "physics_hr_prob": game_hr_prob,
        "density_altitude_ft": _safe_float(env.get("density_altitude_ft"), 0.0),
        "air_density_kg_m3": _safe_float(env.get("air_density_kg_m3"), 1.2),
        "drag_multiplier": _safe_float(env.get("drag_multiplier"), 1.0),
        "wind_out_component_cf": _safe_float(wind_map.get("wind_out_component_cf"), 0.0),
        "wind_out_component_rf": _safe_float(wind_map.get("wind_out_component_rf"), 0.0),
        "wind_out_component_lf": _safe_float(wind_map.get("wind_out_component_lf"), 0.0),
        "pitch_micro_matchup_score": _safe_float(micro.get("pitch_micro_matchup_score"), 1.0),
        "vaa_attack_angle_score": _safe_float(micro.get("vaa_attack_angle_score"), 1.0),
        "estimated_pitcher_vaa": _safe_float(micro.get("estimated_pitcher_vaa"), -5.5),
        "estimated_batter_attack_angle": _safe_float(micro.get("estimated_batter_attack_angle"), 12.0),
        "umpire_catcher_cascade": _safe_float(ump_catch.get("umpire_catcher_cascade"), 1.0),
        "fatigue_index": _safe_float(fatigue.get("fatigue_index"), 0.0),
        "lineup_protection_woba_proxy": _safe_float(fatigue.get("lineup_protection_woba_proxy"), 0.105),
        "spin_decay_rpm": _safe_float(spin_decay.get("spin_decay_rpm"), 0.0),
        "spin_decay_flag": _safe_float(spin_decay.get("spin_decay_flag"), 0.0),
        "context_multiplier": _bounded(context_mult, 0.65, 1.55),
    }


def apply_physics_pipeline_to_live(live_df: pd.DataFrame, statcast_df: pd.DataFrame) -> pd.DataFrame:
    """Apply full physics/context simulation to every batter row."""
    if live_df is None or live_df.empty:
        return live_df

    live = live_df.copy()
    env_cache: Dict[int, Dict[str, float]] = {}
    sim_rows = []

    num_simulations = _safe_int(__import__("os").getenv("PA_MONTE_CARLO_SIMS", 10000), 10000)
    num_simulations = max(1000, num_simulations)

    for _, row in live.iterrows():
        team_mask = live["team_side"] == row.get("team_side") if "team_side" in live.columns else pd.Series([True] * len(live))
        team_df = live[team_mask].copy() if len(live) else live
        projected_pas = _safe_float(row.get("projected_pas"), 3.1)

        sim_rows.append(
            simulate_plate_appearance_probability(
                row=row,
                statcast_df=statcast_df,
                projected_pas=projected_pas,
                live_team_df=team_df,
                env_cache=env_cache,
                num_simulations=num_simulations,
            )
        )

    sim_df = pd.DataFrame(sim_rows)
    return pd.concat([live.reset_index(drop=True), sim_df.reset_index(drop=True)], axis=1)
