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

TEAM_SCHEDULE_CACHE: Dict[str, Dict[str, float]] = {}

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


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(1.0 - a, 0.0)))


def _stadium_coords_from_team_abbr(team_abbr: str) -> Tuple[float, float]:
    key = TEAM_ABBR_TO_STADIUM_KEY.get(str(team_abbr or '').upper(), '')
    stadium = STADIUM_COORDINATES.get(key, {})
    return _safe_float(stadium.get('latitude'), 39.5), _safe_float(stadium.get('longitude'), -98.35)


def compute_circadian_travel_fatigue(team_abbr: str, is_home_game: bool, home_team_abbr: str) -> Dict[str, float]:
    """Estimate batter visual/circadian fatigue from recent travel and rest pattern."""
    cache_key = f"{str(team_abbr).upper()}::{int(bool(is_home_game))}::{str(home_team_abbr).upper()}"
    if cache_key in TEAM_SCHEDULE_CACHE:
        return TEAM_SCHEDULE_CACHE[cache_key]

    out = {
        'circadian_disruption_index': 0.0,
        'visual_fatigue_modifier': 1.0,
        'travel_distance_miles': 0.0,
        'rest_day_count': 1.0,
    }

    try:
        team_lookup = statsapi.lookup_team(team_abbr)
        if not team_lookup:
            TEAM_SCHEDULE_CACHE[cache_key] = out
            return out
        team_id = team_lookup[0]['id']
        end_date = datetime.today().date()
        start_date = end_date - timedelta(days=10)
        sched = statsapi.schedule(start_date=start_date.strftime('%Y-%m-%d'), end_date=end_date.strftime('%Y-%m-%d')) or []
        team_games = [g for g in sched if g.get('home_id') == team_id or g.get('away_id') == team_id]
        if not team_games:
            TEAM_SCHEDULE_CACHE[cache_key] = out
            return out

        team_games = sorted(team_games, key=lambda g: str(g.get('game_datetime', '')))
        recent = team_games[-6:]
        played_dates = []
        venue_points = []
        eastward_miles = 0.0
        total_miles = 0.0

        for g in recent:
            raw_dt = str(g.get('game_datetime', ''))
            if raw_dt:
                try:
                    played_dates.append(datetime.strptime(raw_dt[:10], '%Y-%m-%d').date())
                except Exception:
                    pass
            abbr = g.get('home_abbrev') or g.get('home_team_abbrev') or g.get('home_team') or home_team_abbr
            lat, lon = _stadium_coords_from_team_abbr(abbr or home_team_abbr)
            venue_points.append((lat, lon))

        for i in range(1, len(venue_points)):
            lat1, lon1 = venue_points[i - 1]
            lat2, lon2 = venue_points[i]
            total_miles += _haversine_miles(lat1, lon1, lat2, lon2)
            if lon2 > lon1:
                eastward_miles += _haversine_miles(lat1, lon1, lat2, lon2)

        unique_days = sorted(set(played_dates))
        streak = 0
        if unique_days:
            streak = 1
            for i in range(len(unique_days) - 1, 0, -1):
                if (unique_days[i] - unique_days[i - 1]).days == 1:
                    streak += 1
                else:
                    break
            rest_days = max(0, (end_date - unique_days[-1]).days)
        else:
            rest_days = 1

        cur_lat, cur_lon = _stadium_coords_from_team_abbr(home_team_abbr)
        if venue_points:
            last_lat, last_lon = venue_points[-1]
            same_city_miles = _haversine_miles(last_lat, last_lon, cur_lat, cur_lon)
            total_miles += same_city_miles
            if cur_lon > last_lon:
                eastward_miles += same_city_miles

        disruption = (
            min(total_miles / 2500.0, 1.0) * 34.0
            + min(eastward_miles / 1800.0, 1.0) * 26.0
            + min(max(streak - 3, 0) / 7.0, 1.0) * 24.0
            + (0.0 if rest_days > 0 else 12.0)
        )
        disruption = _bounded(disruption, 0.0, 100.0)
        visual_mod = _bounded(1.02 - (disruption / 240.0), 0.82, 1.02)

        out = {
            'circadian_disruption_index': disruption,
            'visual_fatigue_modifier': visual_mod,
            'travel_distance_miles': float(total_miles),
            'rest_day_count': float(rest_days),
        }
    except Exception:
        pass

    TEAM_SCHEDULE_CACHE[cache_key] = out
    return out


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

    # Batter damage profile against the exact pitch types this pitcher throws most.
    arsenal_matchup_score = 1.0
    try:
        if not pitcher_df.empty and not batter_df.empty and "pitch_type" in pitcher_df.columns and "pitch_type" in batter_df.columns:
            pitcher_usage = pitcher_df.groupby("pitch_type", dropna=False).size().rename("n").reset_index()
            pitcher_usage = pitcher_usage[pitcher_usage["pitch_type"].notna()]
            if not pitcher_usage.empty:
                pitcher_usage["usage_w"] = pitcher_usage["n"] / max(float(pitcher_usage["n"].sum()), 1.0)

                batter_df["launch_speed"] = pd.to_numeric(batter_df.get("launch_speed"), errors="coerce")
                batter_df["launch_angle"] = pd.to_numeric(batter_df.get("launch_angle"), errors="coerce")
                batter_df["estimated_slg_using_speedangle"] = pd.to_numeric(
                    batter_df.get("estimated_slg_using_speedangle"), errors="coerce"
                )
                batter_df["delta_run_exp"] = pd.to_numeric(batter_df.get("delta_run_exp"), errors="coerce")
                batter_df["is_barrel"] = (
                    (batter_df["launch_speed"].fillna(0) >= 98) &
                    batter_df["launch_angle"].fillna(0).between(26, 30)
                ).astype(int)

                def _bases_from_events(series: pd.Series) -> pd.Series:
                    return series.map({
                        "single": 1, "double": 2, "triple": 3, "home_run": 4
                    }).fillna(0)

                batter_df["slug_bases"] = _bases_from_events(batter_df.get("events", pd.Series(index=batter_df.index, dtype=object)))

                batter_pitch = batter_df.groupby("pitch_type", dropna=False).agg(
                    xslg=("estimated_slg_using_speedangle", "mean"),
                    slug=("slug_bases", "mean"),
                    runv=("delta_run_exp", "mean"),
                    barrel=("is_barrel", "mean"),
                ).reset_index()
                batter_pitch["xslg"] = batter_pitch["xslg"].fillna(batter_pitch["slug"]).fillna(0.35)
                batter_pitch["runv"] = batter_pitch["runv"].fillna(0.0) * 100.0

                arsenal = pitcher_usage.merge(batter_pitch, on="pitch_type", how="left")
                if not arsenal.empty:
                    arsenal["xslg"] = arsenal["xslg"].fillna(0.35)
                    arsenal["runv"] = arsenal["runv"].fillna(0.0)
                    arsenal["barrel"] = arsenal["barrel"].fillna(0.07)
                    arsenal["damage_component"] = (
                        1.0
                        + ((arsenal["xslg"] - 0.390) * 0.65)
                        + (arsenal["runv"].clip(-20, 20) / 100.0 * 0.45)
                        + ((arsenal["barrel"] - 0.07) * 1.10)
                    )
                    arsenal_matchup_score = _bounded(
                        float((arsenal["damage_component"] * arsenal["usage_w"]).sum()),
                        0.82,
                        1.30,
                    )
    except Exception:
        arsenal_matchup_score = 1.0

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
        "pitch_arsenal_matchup_score": arsenal_matchup_score,
        "vaa_attack_angle_score": vaa_attack_score,
        "estimated_pitcher_vaa": pitch_vaa,
        "estimated_batter_attack_angle": attack_angle,
    }


def compute_umpire_catcher_cascade(game_pk: int, pitcher_id: int, batter_id: int, batter_hand: str, statcast_df: pd.DataFrame) -> Dict[str, float]:
    """Estimate strike-zone pressure from umpire profile plus catcher framing tendency.

    Includes a simple spatial miss-zone model that measures whether an umpire's
    likely drift overlaps the batter hot zone or helps the pitcher live on the edges.
    """
    umpire_impact = 1.0
    catcher_framing_impact = 1.0
    zone_drift_score = 1.0
    hotzone_overlap = 0.0
    hp_name = ""

    try:
        g = statsapi.get("game", {"gamePk": game_pk})
        officials = g.get("liveData", {}).get("boxscore", {}).get("officials", [])
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

    try:
        if statcast_df is not None and not statcast_df.empty:
            p_df = statcast_df[pd.to_numeric(statcast_df.get("pitcher"), errors="coerce") == int(pitcher_id)].copy()
            b_df = statcast_df[pd.to_numeric(statcast_df.get("batter"), errors="coerce") == int(batter_id)].copy()
            if not p_df.empty and not b_df.empty:
                p_df["plate_x"] = pd.to_numeric(p_df.get("plate_x"), errors="coerce")
                p_df["plate_z"] = pd.to_numeric(p_df.get("plate_z"), errors="coerce")
                b_df["plate_x"] = pd.to_numeric(b_df.get("plate_x"), errors="coerce")
                b_df["plate_z"] = pd.to_numeric(b_df.get("plate_z"), errors="coerce")
                b_df["launch_speed"] = pd.to_numeric(b_df.get("launch_speed"), errors="coerce")
                b_df["launch_angle"] = pd.to_numeric(b_df.get("launch_angle"), errors="coerce")

                called = p_df[p_df.get("description").isin(["called_strike", "ball"])].copy()
                if called.empty:
                    called = p_df.copy()

                hot = b_df[(b_df["launch_speed"].fillna(0) >= 95) & b_df["launch_angle"].fillna(0).between(18, 35)].copy()
                if hot.empty:
                    hot = b_df[b_df["plate_x"].notna() & b_df["plate_z"].notna()].copy()

                batter_x = float(hot["plate_x"].mean()) if not hot.empty else (-0.12 if str(batter_hand).upper() == "L" else 0.12)
                batter_z = float(hot["plate_z"].mean()) if not hot.empty else 2.65

                pitcher_x = float(called["plate_x"].mean()) if called["plate_x"].notna().any() else 0.0
                pitcher_z = float(called["plate_z"].mean()) if called["plate_z"].notna().any() else 2.45

                lower = hp_name.lower()
                drift_x = 0.0
                drift_z = 0.0
                squeeze = 0.0
                if any(name in lower for name in ("bucknor", "west", "bellino")):
                    drift_z = -0.22
                    squeeze = -0.08
                elif any(name in lower for name in ("layne", "lee", "hernandez")):
                    drift_z = 0.08
                    squeeze = 0.10

                if str(batter_hand).upper() == "L":
                    drift_x = -0.06
                else:
                    drift_x = 0.06

                drifted_x = pitcher_x + drift_x
                drifted_z = pitcher_z + drift_z
                dist = math.sqrt(((drifted_x - batter_x) / 0.85) ** 2 + ((drifted_z - batter_z) / 0.75) ** 2)
                hotzone_overlap = float(math.exp(-(dist ** 2)))

                low_pitcher_bias = _bounded((2.35 - pitcher_z) / 0.9, 0.0, 1.0)
                low_hotzone_penalty = _bounded((batter_z - 2.3) / 0.7, 0.0, 1.0)
                zone_drift_score = 1.0 + (hotzone_overlap * 0.14) + squeeze - (low_pitcher_bias * low_hotzone_penalty * max(-drift_z, 0.0) * 0.45)
                zone_drift_score = _bounded(zone_drift_score, 0.84, 1.16)
    except Exception:
        pass

    return {
        "umpire_strike_zone_multiplier": umpire_impact,
        "catcher_framing_multiplier": catcher_framing_impact,
        "umpire_zone_drift_score": zone_drift_score,
        "umpire_hotzone_overlap": hotzone_overlap,
        "umpire_catcher_cascade": _bounded(umpire_impact * catcher_framing_impact * zone_drift_score, 0.85, 1.18),
    }


def compute_fatigue_and_lineup_protection(
    batter_id: int,
    batting_order_slot: int,
    live_team_df: pd.DataFrame,
    statcast_df: pd.DataFrame,
    team_abbr: str,
    is_home_game: bool,
    home_team_abbr: str,
) -> Dict[str, float]:
    """Estimate biomechanical fatigue and lineup protection from recent usage."""
    fatigue_index = 0.0
    fatigue_multiplier = 1.0
    circadian_idx = 0.0
    visual_modifier = 1.0
    travel_miles = 0.0
    rest_days = 1.0

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

    try:
        circadian = compute_circadian_travel_fatigue(team_abbr=team_abbr, is_home_game=is_home_game, home_team_abbr=home_team_abbr)
        circadian_idx = _safe_float(circadian.get('circadian_disruption_index'), 0.0)
        visual_modifier = _safe_float(circadian.get('visual_fatigue_modifier'), 1.0)
        travel_miles = _safe_float(circadian.get('travel_distance_miles'), 0.0)
        rest_days = _safe_float(circadian.get('rest_day_count'), 1.0)
        fatigue_index = _bounded(fatigue_index + (circadian_idx * 0.45), 0, 100)
        fatigue_multiplier = _bounded(fatigue_multiplier * visual_modifier, 0.78, 1.06)
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
        "circadian_disruption_index": circadian_idx,
        "visual_fatigue_modifier": visual_modifier,
        "travel_distance_miles": travel_miles,
        "rest_day_count": rest_days,
        "lineup_protection_woba_proxy": avg_woba,
        "lineup_protection_multiplier": lineup_multiplier,
    }


def compute_pitcher_spin_decay(pitcher_id: int, statcast_df: pd.DataFrame) -> Dict[str, float]:
    """Compute pitch-leak/fatigue metrics from recent pitch-by-pitch decay.

    Tracks:
    - Rolling release-point variance over the latest game
    - Spin-to-velocity ratio decay for the primary weapon
    - Approximate pitch-count threshold where the primary weapon becomes vulnerable
    - Cross-game spin decay over recent starts
    """
    if statcast_df is None or statcast_df.empty:
        return {
            "spin_decay_rpm": 0.0,
            "spin_decay_multiplier": 1.0,
            "spin_decay_flag": 0,
            "release_pos_x_std_15": 0.0,
            "release_pos_z_std_15": 0.0,
            "release_extension_decay_ft": 0.0,
            "spin_velocity_ratio_decay": 0.0,
            "primary_weapon_vulnerable_pitch_count": 0,
        }

    try:
        pdf = statcast_df[pd.to_numeric(statcast_df.get("pitcher"), errors="coerce") == int(pitcher_id)].copy()
        if pdf.empty:
            raise ValueError("empty pitcher sample")

        pdf["release_spin_rate"] = pd.to_numeric(pdf.get("release_spin_rate"), errors="coerce")
        pdf["release_speed"] = pd.to_numeric(pdf.get("release_speed"), errors="coerce")
        pdf["release_extension"] = pd.to_numeric(pdf.get("release_extension"), errors="coerce")
        pdf["release_pos_x"] = pd.to_numeric(pdf.get("release_pos_x"), errors="coerce")
        pdf["release_pos_z"] = pd.to_numeric(pdf.get("release_pos_z"), errors="coerce")
        pdf["release_spin_rate"] = pd.to_numeric(pdf.get("release_spin_rate"), errors="coerce")
        pdf["game_date"] = pd.to_datetime(pdf.get("game_date"), errors="coerce")
        pdf = pdf.dropna(subset=["release_spin_rate", "release_speed", "game_date"])
        if pdf.empty:
            raise ValueError("empty spin sample")

        primary_pitch = str(pdf.get("pitch_type", pd.Series(dtype=object)).mode().iloc[0]) if "pitch_type" in pdf.columns and not pdf.get("pitch_type", pd.Series(dtype=object)).dropna().empty else "FF"
        primary_pdf = pdf[pdf.get("pitch_type") == primary_pitch].copy()
        if primary_pdf.empty:
            primary_pdf = pdf.copy()

        by_game = primary_pdf.groupby(primary_pdf["game_date"].dt.date)["release_spin_rate"].mean().reset_index()
        by_game = by_game.sort_values("game_date")

        recent = by_game.tail(3)["release_spin_rate"].mean()
        baseline = by_game.head(max(len(by_game) - 3, 1))["release_spin_rate"].mean()
        decay = float(baseline - recent)

        latest_game_date = primary_pdf["game_date"].max()
        latest_game = primary_pdf[primary_pdf["game_date"] == latest_game_date].copy()
        latest_game = latest_game.sort_values([c for c in ["at_bat_number", "pitch_number"] if c in latest_game.columns])
        latest_game = latest_game.reset_index(drop=True)
        latest_game["pitch_seq"] = np.arange(1, len(latest_game) + 1)
        latest_game["spin_velocity_ratio"] = latest_game["release_spin_rate"] / latest_game["release_speed"].clip(lower=1.0)

        rel_x_std = latest_game["release_pos_x"].rolling(15, min_periods=5).std().fillna(0.0)
        rel_z_std = latest_game["release_pos_z"].rolling(15, min_periods=5).std().fillna(0.0)
        ext_roll = latest_game["release_extension"].rolling(15, min_periods=5).mean().fillna(method="bfill").fillna(6.0)
        svr_roll = latest_game["spin_velocity_ratio"].rolling(15, min_periods=5).mean().fillna(method="bfill").fillna(24.0)

        baseline_slice = latest_game.head(min(20, len(latest_game)))
        baseline_ext = float(baseline_slice["release_extension"].mean()) if not baseline_slice.empty else 6.0
        baseline_svr = float(baseline_slice["spin_velocity_ratio"].mean()) if not baseline_slice.empty else 24.0

        ext_decay = float(max(0.0, baseline_ext - float(ext_roll.iloc[-1]) if len(ext_roll) else 0.0))
        svr_decay = float(max(0.0, baseline_svr - float(svr_roll.iloc[-1]) if len(svr_roll) else 0.0))
        rel_x_now = float(rel_x_std.iloc[-1]) if len(rel_x_std) else 0.0
        rel_z_now = float(rel_z_std.iloc[-1]) if len(rel_z_std) else 0.0

        leak_mask = latest_game["pitch_seq"] >= 45
        leak_mask &= (
            (rel_x_std >= 0.18) |
            (rel_z_std >= 0.18) |
            (svr_roll <= (baseline_svr - 0.90)) |
            (ext_roll <= (baseline_ext - 0.18))
        )
        vulnerable_pitch = int(latest_game.loc[leak_mask, "pitch_seq"].iloc[0]) if leak_mask.any() else 0

        flag = 1 if (decay >= 50 or vulnerable_pitch > 0) else 0
        mult = _bounded(
            1.0 + (max(decay, 0.0) / 550.0) + (rel_x_now * 0.30) + (rel_z_now * 0.25) + (svr_decay * 0.08) + (ext_decay * 0.10),
            1.0,
            1.30,
        )
        return {
            "spin_decay_rpm": decay,
            "spin_decay_multiplier": mult,
            "spin_decay_flag": flag,
            "release_pos_x_std_15": rel_x_now,
            "release_pos_z_std_15": rel_z_now,
            "release_extension_decay_ft": ext_decay,
            "spin_velocity_ratio_decay": svr_decay,
            "primary_weapon_vulnerable_pitch_count": vulnerable_pitch,
        }
    except Exception:
        return {
            "spin_decay_rpm": 0.0,
            "spin_decay_multiplier": 1.0,
            "spin_decay_flag": 0,
            "release_pos_x_std_15": 0.0,
            "release_pos_z_std_15": 0.0,
            "release_extension_decay_ft": 0.0,
            "spin_velocity_ratio_decay": 0.0,
            "primary_weapon_vulnerable_pitch_count": 0,
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
        batter_id=_safe_int(row.get("batter", 0), 0),
        batter_hand=str(row.get("batter_hand", "R") or "R"),
        statcast_df=statcast_df,
    )

    fatigue = compute_fatigue_and_lineup_protection(
        batter_id=_safe_int(row.get("batter", 0), 0),
        batting_order_slot=_safe_int(row.get("batting_order_slot", 5), 5),
        live_team_df=live_team_df,
        statcast_df=statcast_df,
        team_abbr=str(row.get("home_team") if bool(row.get("is_home_game", False)) else row.get("away_team")),
        is_home_game=bool(row.get("is_home_game", False)),
        home_team_abbr=str(row.get("home_team", "")),
    )

    spin_decay = compute_pitcher_spin_decay(
        pitcher_id=_safe_int(row.get("pitcher", 0), 0),
        statcast_df=statcast_df,
    )

    opp_bullpen_score = _safe_float(
        row.get("bullpen_quality_score_away") if bool(row.get("is_home_game", False)) else row.get("bullpen_quality_score_home"),
        50.0,
    )
    bullpen_pa_share = _bounded(projected_pas / 10.0, 0.32, 0.48)
    bullpen_exposure_multiplier = _bounded(1.0 + (((opp_bullpen_score - 50.0) / 100.0) * bullpen_pa_share * 0.22), 0.94, 1.12)

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
        * _safe_float(micro.get("pitch_arsenal_matchup_score"), 1.0)
        * _safe_float(micro.get("vaa_attack_angle_score"), 1.0)
        * _safe_float(ump_catch.get("umpire_catcher_cascade"), 1.0)
        * _safe_float(fatigue.get("fatigue_multiplier"), 1.0)
        * _safe_float(fatigue.get("lineup_protection_multiplier"), 1.0)
        * _safe_float(spin_decay.get("spin_decay_multiplier"), 1.0)
        * bullpen_exposure_multiplier
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
        "pitch_arsenal_matchup_score": _safe_float(micro.get("pitch_arsenal_matchup_score"), 1.0),
        "vaa_attack_angle_score": _safe_float(micro.get("vaa_attack_angle_score"), 1.0),
        "estimated_pitcher_vaa": _safe_float(micro.get("estimated_pitcher_vaa"), -5.5),
        "estimated_batter_attack_angle": _safe_float(micro.get("estimated_batter_attack_angle"), 12.0),
        "umpire_catcher_cascade": _safe_float(ump_catch.get("umpire_catcher_cascade"), 1.0),
        "umpire_zone_drift_score": _safe_float(ump_catch.get("umpire_zone_drift_score"), 1.0),
        "umpire_hotzone_overlap": _safe_float(ump_catch.get("umpire_hotzone_overlap"), 0.0),
        "fatigue_index": _safe_float(fatigue.get("fatigue_index"), 0.0),
        "circadian_disruption_index": _safe_float(fatigue.get("circadian_disruption_index"), 0.0),
        "visual_fatigue_modifier": _safe_float(fatigue.get("visual_fatigue_modifier"), 1.0),
        "travel_distance_miles": _safe_float(fatigue.get("travel_distance_miles"), 0.0),
        "rest_day_count": _safe_float(fatigue.get("rest_day_count"), 1.0),
        "lineup_protection_woba_proxy": _safe_float(fatigue.get("lineup_protection_woba_proxy"), 0.105),
        "spin_decay_rpm": _safe_float(spin_decay.get("spin_decay_rpm"), 0.0),
        "spin_decay_flag": _safe_float(spin_decay.get("spin_decay_flag"), 0.0),
        "release_pos_x_std_15": _safe_float(spin_decay.get("release_pos_x_std_15"), 0.0),
        "release_pos_z_std_15": _safe_float(spin_decay.get("release_pos_z_std_15"), 0.0),
        "release_extension_decay_ft": _safe_float(spin_decay.get("release_extension_decay_ft"), 0.0),
        "spin_velocity_ratio_decay": _safe_float(spin_decay.get("spin_velocity_ratio_decay"), 0.0),
        "primary_weapon_vulnerable_pitch_count": _safe_float(spin_decay.get("primary_weapon_vulnerable_pitch_count"), 0.0),
        "bullpen_exposure_multiplier": bullpen_exposure_multiplier,
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
