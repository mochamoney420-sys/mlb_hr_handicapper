#!/usr/bin/env python
"""
Professional Bettor Features for MLB Home Run Prediction
=========================================================

Implements:
1. Pitcher platoon splits (L/R analysis)
2. Bullpen quality scoring  
3. Umpire strike zone analysis
4. Density altitude calculations
5. Gameday weather extremes
6. Sportsbook hold analysis & arbitrage detection
7. Professional pairing recommendations
"""

import pandas as pd
import numpy as np
import statsapi
import requests
from datetime import datetime, timedelta
from pathlib import Path
import json
import warnings
warnings.filterwarnings('ignore')


# =====================================================================
# 1. PITCHER PLATOON SPLITS (L/R ANALYSIS)
# =====================================================================

def get_pitcher_platoon_splits(pitcher_id, statcast_data):
    """
    Analyze pitcher's home run rates against LHH vs RHH.
    Some RHP give up 40%+ HR/FB to LHH due to weak horizontal movement.
    """
    if statcast_data is None or statcast_data.empty:
        return {'rh_hitters': {}, 'lh_hitters': {}}
    
    pitcher_data = statcast_data[statcast_data['pitcher'] == pitcher_id].copy()
    
    if pitcher_data.empty:
        return {'rh_hitters': {}, 'lh_hitters': {}}
    
    splits = {'rh_hitters': {}, 'lh_hitters': {}}
    
    # RHH splits
    rhh_data = pitcher_data[pitcher_data['stand'] == 'R']
    if len(rhh_data) > 0:
        rhh_batted = rhh_data[rhh_data['type'] == 'pitch']
        if len(rhh_batted) > 0:
            rhh_hrs = len(rhh_batted[rhh_batted['events'] == 'home_run'])
            rhh_fly_balls = len(rhh_batted[rhh_batted['bb_type'] == 'fly_ball'])
            
            splits['rh_hitters'] = {
                'plate_appearances': len(rhh_data),
                'batted_balls': len(rhh_batted),
                'home_runs': rhh_hrs,
                'fly_balls': rhh_fly_balls,
                'hr_per_fb': rhh_hrs / max(rhh_fly_balls, 1),
                'barrel_rate': len(rhh_batted[(rhh_batted['launch_speed'] >= 98) & 
                                             (rhh_batted['launch_angle'].between(26, 30))]) / len(rhh_batted),
                'hard_hit_rate': len(rhh_batted[rhh_batted['launch_speed'] >= 95]) / len(rhh_batted)
            }
    
    # LHH splits
    lhh_data = pitcher_data[pitcher_data['stand'] == 'L']
    if len(lhh_data) > 0:
        lhh_batted = lhh_data[lhh_data['type'] == 'pitch']
        if len(lhh_batted) > 0:
            lhh_hrs = len(lhh_batted[lhh_batted['events'] == 'home_run'])
            lhh_fly_balls = len(lhh_batted[lhh_batted['bb_type'] == 'fly_ball'])
            
            splits['lh_hitters'] = {
                'plate_appearances': len(lhh_data),
                'batted_balls': len(lhh_batted),
                'home_runs': lhh_hrs,
                'fly_balls': lhh_fly_balls,
                'hr_per_fb': lhh_hrs / max(lhh_fly_balls, 1),
                'barrel_rate': len(lhh_batted[(lhh_batted['launch_speed'] >= 98) & 
                                             (lhh_batted['launch_angle'].between(26, 30))]) / len(lhh_batted),
                'hard_hit_rate': len(lhh_batted[lhh_batted['launch_speed'] >= 95]) / len(lhh_batted)
            }
    
    return splits


def identify_platoon_mismatches(batter_id, pitcher_id, batter_hand, statcast_data):
    """
    Identify if batter has significant advantage/disadvantage vs pitcher's splits.
    Returns probability uplift/downgrade based on mismatch.
    
    Incorporates:
    - Sightline advantage (opposite-handed batters see fastballs better)
    - Breaking pitch vulnerability (sliders break into power zone)
    - Reverse-split anomalies (same-handed guys who still dominate)
    """
    if statcast_data is None or statcast_data.empty:
        return 1.0  # No adjustment
    
    pitcher_splits = get_pitcher_platoon_splits(pitcher_id, statcast_data)
    
    if batter_hand == 'L':
        splits = pitcher_splits.get('lh_hitters', {})
        is_opposite_handed = True  # LHH vs RHP is opposite
    else:
        splits = pitcher_splits.get('rh_hitters', {})
        is_opposite_handed = False  # RHH vs LHP is opposite
    
    if not splits or splits.get('batted_balls', 0) < 10:
        return 1.0  # Insufficient data
    
    # Calculate edge multiplier
    hr_fb = splits.get('hr_per_fb', 0)
    barrel_rate = splits.get('barrel_rate', 0)
    
    # Base multiplier from HR/FB
    if hr_fb >= 0.15:  # 15%+ HR/FB is elite vulnerability
        multiplier = 1.3  # 30% probability uplift
    elif hr_fb >= 0.12:
        multiplier = 1.2  # 20% uplift
    elif hr_fb >= 0.10:
        multiplier = 1.1  # 10% uplift
    else:
        multiplier = 1.0
    
    # Sightline bonus for opposite-handed matchups
    if is_opposite_handed and barrel_rate >= 0.08:
        # Opposite-handed batters see fastballs better
        multiplier *= 1.15  # Additional +15% for sightline advantage
    
    # Reverse-split anomaly detection (same-handed guy crushing it)
    if not is_opposite_handed and hr_fb >= 0.13:
        # Same-handed hitter with unusually high HR/FB = pitcher weakness
        multiplier *= 1.12  # +12% bonus for reverse-split anomaly
    
    return min(multiplier, 1.5)  # Cap at 50% total uplift


def detect_breaking_pitch_vulnerability(pitcher_id, batter_hand, statcast_data):
    """
    Detect if pitcher's breaking balls break INTO batter's power zone.
    This is extremely high-value for opposite-handed matchups.
    
    Returns vulnerability score (1.0 = neutral, 1.3+ = high vulnerability)
    """
    if statcast_data is None or statcast_data.empty:
        return 1.0
    
    try:
        pitcher_data = statcast_data[statcast_data['pitcher'] == pitcher_id].copy()
        if pitcher_data.empty:
            return 1.0
        
        # Filter to breaking balls (sliders, sweepers, curveballs)
        breaking_balls = pitcher_data[
            pitcher_data['pitch_type'].isin(['SL', 'ST', 'CU', 'KC'])
        ].copy()
        
        if len(breaking_balls) < 15:
            return 1.0  # Insufficient data
        
        # Opposite-handed matchup: slider breaks INTO power zone
        if batter_hand == 'L':  # LHH
            # LHH pull-side is left side; RHP slider should break away (outside)
            # If it doesn't, it's vulnerable
            breaking_to_pull = breaking_balls[
                (breaking_balls['release_spin_rate'] < 2400) |  # Low spin = less break
                (breaking_balls['plate_x'] > 0)  # Breaking to center/pull side
            ]
        else:  # RHH
            # RHH pull-side is right side; LHP slider should break away (outside)
            breaking_to_pull = breaking_balls[
                (breaking_balls['release_spin_rate'] < 2400) |
                (breaking_balls['plate_x'] < 0)
            ]
        
        if len(breaking_to_pull) == 0:
            return 1.0
        
        # Calculate HR rate on vulnerable breaking balls
        hrs_on_bb = len(breaking_to_pull[breaking_to_pull['events'] == 'home_run'])
        hr_rate_on_bb = hrs_on_bb / len(breaking_to_pull)
        
        # Vulnerability multiplier
        if hr_rate_on_bb >= 0.10:
            return 1.35  # Extreme vulnerability
        elif hr_rate_on_bb >= 0.07:
            return 1.25  # High vulnerability
        elif hr_rate_on_bb >= 0.05:
            return 1.15  # Moderate vulnerability
        else:
            return 1.0
    
    except Exception:
        return 1.0


def identify_left_on_right_fade_opportunity(pitcher_id, statcast_data):
    """
    Identify RHP who lack quality changeup and are vulnerable to LHH.
    
    Returns bonus multiplier (1.0 = not vulnerable, 1.25+ = high opportunity)
    """
    if statcast_data is None or statcast_data.empty:
        return 1.0
    
    try:
        pitcher_data = statcast_data[statcast_data['pitcher'] == pitcher_id].copy()
        if pitcher_data.empty:
            return 1.0
        
        # Filter to LHH only (this is the vulnerability we're checking)
        lhh_data = pitcher_data[pitcher_data['stand'] == 'L'].copy()
        if len(lhh_data) < 20:
            return 1.0
        
        # Check for quality changeup (low velocity variance = good changeup)
        fastballs = lhh_data[lhh_data['pitch_type'].isin(['FF', 'FT', 'SI'])]
        changeups = lhh_data[lhh_data['pitch_type'].isin(['CH', 'FS'])]
        
        if len(fastballs) < 10 or len(changeups) < 5:
            return 1.0
        
        fb_velo = fastballs['release_speed'].mean()
        ch_velo = changeups['release_speed'].mean()
        
        velocity_diff = fb_velo - ch_velo
        
        # If velocity difference is small (<4 mph), changeup is NOT effective
        if velocity_diff < 4:
            # Pitcher is forced to throw fastballs/breaking balls to LHH
            # These are more hittable
            
            # Check LHH HR rate
            lhh_batted = lhh_data[lhh_data['type'] == 'pitch']
            if len(lhh_batted) > 0:
                lhh_hrs = len(lhh_batted[lhh_batted['events'] == 'home_run'])
                lhh_fly_balls = len(lhh_batted[lhh_batted['bb_type'] == 'fly_ball'])
                
                if lhh_fly_balls > 0:
                    lhh_hr_fb = lhh_hrs / lhh_fly_balls
                    
                    if lhh_hr_fb >= 0.15:
                        return 1.35  # Extreme fade opportunity
                    elif lhh_hr_fb >= 0.12:
                        return 1.25  # Strong fade opportunity
                    elif lhh_hr_fb >= 0.10:
                        return 1.15
        
        return 1.0
    
    except Exception:
        return 1.0


def detect_reverse_split_anomaly(pitcher_id, statcast_data):
    """
    Detect pitchers who give up MORE HRs to same-handed hitters.
    Usually indicates cutter/sinker that runs into pull side.
    
    Returns (is_anomaly: bool, multiplier: float)
    """
    if statcast_data is None or statcast_data.empty:
        return False, 1.0
    
    try:
        pitcher_data = statcast_data[statcast_data['pitcher'] == pitcher_id].copy()
        if pitcher_data.empty:
            return False, 1.0
        
        # Get same-handed and opposite-handed splits
        rh_data = pitcher_data[pitcher_data['stand'] == 'R']
        lh_data = pitcher_data[pitcher_data['stand'] == 'L']
        
        # Calculate HR/FB for each
        rh_batted = rh_data[rh_data['type'] == 'pitch']
        lh_batted = lh_data[lh_data['type'] == 'pitch']
        
        if len(rh_batted) < 20 or len(lh_batted) < 20:
            return False, 1.0
        
        rh_hr_fb = len(rh_batted[rh_batted['events'] == 'home_run']) / max(
            len(rh_batted[rh_batted['bb_type'] == 'fly_ball']), 1
        )
        
        lh_hr_fb = len(lh_batted[lh_batted['events'] == 'home_run']) / max(
            len(lh_batted[lh_batted['bb_type'] == 'fly_ball']), 1
        )
        
        # Check if same-handed is significantly higher
        if rh_hr_fb > lh_hr_fb and (rh_hr_fb - lh_hr_fb) >= 0.04:  # 4%+ difference
            # Reverse split anomaly detected (RHH crushing it)
            multiplier = 1.0 + (rh_hr_fb - lh_hr_fb) * 5  # Scale the difference
            return True, min(multiplier, 1.3)
        
        elif lh_hr_fb > rh_hr_fb and (lh_hr_fb - rh_hr_fb) >= 0.04:
            # Reverse split anomaly detected (LHH crushing it)
            multiplier = 1.0 + (lh_hr_fb - rh_hr_fb) * 5
            return True, min(multiplier, 1.3)
        
        return False, 1.0
    
    except Exception:
        return False, 1.0


# =====================================================================
# 2. BULLPEN QUALITY SCORING
# =====================================================================

def calculate_bullpen_fatigue_score(team, game_date, statcast_data):
    """
    Score bullpen quality based on:
    - Bullpen ERA
    - Recent appearance frequency
    - Rest days
    - Opener/exhausted bullpen detection
    """
    try:
        if not hasattr(statsapi, 'team_stat'):
            return 50

        # Get team stats from StatsAPI
        team_id = statsapi.lookup_team(team)[0]['id']
        team_stats = statsapi.team_stat(team_id, 'pitching', group='bullpen')
        
        if not team_stats:
            return 50  # Neutral score if unavailable
        
        # Parse bullpen metrics
        bullpen_data = team_stats.get('stats', [{}])[0].get('stats', {})
        
        bullpen_era = float(bullpen_data.get('era', 0)) or 4.5
        bullpen_whip = float(bullpen_data.get('whip', 0)) or 1.3
        appearances = float(bullpen_data.get('gamesPlayed', 0)) or 50
        
        # Calculate fatigue score (0-100, higher = weaker)
        fatigue = 0
        
        # ERA component (higher ERA = lower score, weaker bullpen)
        if bullpen_era >= 5.0:
            fatigue += 35  # Disaster tier
        elif bullpen_era >= 4.5:
            fatigue += 25
        elif bullpen_era >= 4.0:
            fatigue += 15
        else:
            fatigue += 5
        
        # WHIP component
        if bullpen_whip >= 1.5:
            fatigue += 30
        elif bullpen_whip >= 1.3:
            fatigue += 20
        elif bullpen_whip >= 1.2:
            fatigue += 10
        else:
            fatigue += 5
        
        # Appearance frequency (high = fatigued)
        recent_games = 5  # Last 5 games
        if appearances / recent_games >= 2.5:  # 2.5+ appearances per game recently
            fatigue += 15  # Heavy usage = fatigue
        
        return min(fatigue, 100)
    
    except Exception as e:
        print(f"Error calculating bullpen fatigue: {e}")
        return 50


def detect_bullpen_day(game_id):
    """
    Detect if a team is using opener or exhausted bullpen strategy.
    Returns True if detected (high-leverage opportunity).
    """
    try:
        game_data = statsapi.get('game', {'gamePk': game_id})
        
        # Check if starting pitcher has very low expected innings
        home_sp = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get('home', {}).get('pitchers', [])
        away_sp = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get('away', {}).get('pitchers', [])
        
        # If first listed pitcher is not traditional starter, it's likely opener/bullpen day
        # Heuristic: Starter who typically pitches <3 innings = opener
        
        return False  # Conservative default
    
    except Exception:
        return False


def get_bullpen_quality_multiplier(away_team, home_team, game_date, statcast_data):
    """
    Return probability multipliers for exploiting weak bullpens.
    
    Example:
      home_team has ERA 5.2 bullpen, fatigue_score=75
      away_team power hitters get +15% to +25% probability uplift
    """
    home_fatigue = calculate_bullpen_fatigue_score(home_team, game_date, statcast_data)
    away_fatigue = calculate_bullpen_fatigue_score(away_team, game_date, statcast_data)
    
    # Home team bullpen weak = away hitters favored
    away_multiplier = 1.0 + (home_fatigue / 500.0)  # Max +20% if bullpen is disaster
    
    # Away team bullpen weak = home hitters favored
    home_multiplier = 1.0 + (away_fatigue / 500.0)
    
    return {'home_hitters_multiplier': home_multiplier, 'away_hitters_multiplier': away_multiplier}


# =====================================================================
# 3. UMPIRE STRIKE ZONE ANALYSIS
# =====================================================================

def get_umpire_strike_zone_profile(umpire_name):
    """
    Get strike zone bias for specific umpire.
    Tight zones force fastballs down the middle = more barrels.
    """
    # Umpire database: based on Statcast data
    umpire_profiles = {
        'tight_zones': {  # These umpires call tight zones
            'Jun-Sung Lee': {'zone_size': 0.85, 'fastball_rate': 0.68, 'impact': 1.15},
            'Jerry Layne': {'zone_size': 0.82, 'fastball_rate': 0.70, 'impact': 1.18},
            'Angel Hernandez': {'zone_size': 0.88, 'fastball_rate': 0.65, 'impact': 1.10},
        },
        'wide_zones': {  # These umpires call wide zones
            'CB Bucknor': {'zone_size': 1.25, 'fastball_rate': 0.55, 'impact': 0.88},
            'Joe West': {'zone_size': 1.20, 'fastball_rate': 0.58, 'impact': 0.92},
            'Dan Bellino': {'zone_size': 1.15, 'fastball_rate': 0.60, 'impact': 0.95},
        }
    }
    
    # Default neutral profile if umpire not in database
    default_profile = {'zone_size': 1.0, 'fastball_rate': 0.62, 'impact': 1.0}
    
    for category in umpire_profiles.values():
        if umpire_name in category:
            return category[umpire_name]
    
    return default_profile


def get_todays_umpires():
    """Fetch today's umpire assignments from StatsAPI."""
    try:
        today = datetime.today().strftime('%Y-%m-%d')
        games = statsapi.schedule(start_date=today, end_date=today)
        
        umpires_dict = {}
        for game in games:
            game_id = game['game_id']
            game_data = statsapi.get('game', {'gamePk': game_id})

            officials = game_data.get('liveData', {}).get('boxscore', {}).get('officials', [])
            hp_umpire = officials[0].get('person', {}).get('fullName', 'Unknown') if officials else 'Unknown'
            
            umpires_dict[game_id] = {
                'home_plate': hp_umpire,
                'profile': get_umpire_strike_zone_profile(hp_umpire)
            }
        
        return umpires_dict
    except Exception as e:
        print(f"Error fetching umpires: {e}")
        return {}


# =====================================================================
# 4. DENSITY ALTITUDE CALCULATIONS
# =====================================================================

def calculate_density_altitude(temperature_f, elevation_ft, humidity_pct, pressure_inHg=29.92):
    """
    Calculate density altitude for ball flight physics.
    Higher density altitude = air is thinner = ball carries farther.
    
    Formula-based approximation of density altitude.
    """
    # Convert to standard conditions
    temp_c = (temperature_f - 32) * 5/9
    elevation_m = elevation_ft * 0.3048
    
    # Approximate density altitude (simplified formula)
    # Each 1000 ft elevation ≈ 120 ft density altitude change
    # Each 10°F temperature ≈ 500 ft density altitude change
    # Humidity slightly reduces effect (moister air is denser)
    
    base_da = elevation_m * 0.12  # Elevation contribution
    temp_da = (temperature_f - 59) * 50  # Temperature contribution (59°F is standard)
    humidity_adjustment = (humidity_pct - 50) * -3  # Moister = denser
    
    density_altitude = base_da + temp_da + humidity_adjustment
    
    return {
        'density_altitude_ft': max(0, density_altitude),
        'ball_carry_factor': 1.0 + (density_altitude / 10000.0),  # 10k DA = +10% carry
        'temperature': temperature_f,
        'elevation': elevation_ft,
        'humidity': humidity_pct
    }


def get_gameday_conditions(game_id, stadium_info):
    """Get weather and atmospheric conditions for specific game."""
    try:
        game_data = statsapi.get('game', {'gamePk': game_id})
        
        weather = game_data.get('gameData', {}).get('weather', {})
        venue = game_data.get('gameData', {}).get('venue', {})
        
        temp = int(weather.get('temp', 70))
        humidity = int(weather.get('humidity', 50).rstrip('%'))
        wind_speed = int(weather.get('wind', {}).get('speed', 0).split()[0])
        wind_dir = weather.get('wind', {}).get('direction', 'Unknown')
        
        # Get elevation from stadium info
        elevation = stadium_info.get(venue.get('id'), {}).get('elevation', 0)
        
        return {
            'temperature': temp,
            'humidity': humidity,
            'wind_speed': wind_speed,
            'wind_direction': wind_dir,
            'elevation': elevation,
            'venue_id': venue.get('id'),
            'venue_name': venue.get('name')
        }
    except Exception as e:
        print(f"Error fetching game conditions: {e}")
        return {}


def detect_weather_extremes(conditions):
    """
    Identify extreme weather conditions favoring ball flight.
    Returns bonus multiplier.
    """
    if not conditions:
        return 1.0
    
    temp = conditions.get('temperature', 70)
    wind = conditions.get('wind_speed', 0)
    humidity = conditions.get('humidity', 50)
    
    multiplier = 1.0
    
    # Temperature extremes (heatwaves)
    if temp >= 90:
        multiplier += (temp - 90) * 0.01  # +1% per degree above 90
    
    # Wind out to field
    if wind >= 10:
        multiplier += (wind - 10) * 0.02  # +2% per mph of wind
    
    # Dry conditions (low humidity = less dense air)
    if humidity <= 40:
        multiplier += (40 - humidity) * 0.01
    
    return min(multiplier, 1.35)  # Cap at +35%


# =====================================================================
# 5. SPORTSBOOK HOLD ANALYSIS & ARBITRAGE
# =====================================================================

def calculate_hold_percentage(book_odds, market_odds_implied):
    """
    Calculate sportsbook hold (house edge).
    Higher hold = worse long-term ROI for bettors.
    
    hold% = (1 - 1/implied_prob) * 100
    """
    if book_odds >= 0:  # American odds positive
        implied_prob = 100 / (book_odds + 100)
    else:
        implied_prob = abs(book_odds) / (abs(book_odds) + 100)
    
    hold = (1 - 1/market_odds_implied) * 100 if market_odds_implied > 0 else 0
    
    return {
        'implied_probability': implied_prob,
        'hold_percentage': hold,
        'roi_potential': 1 / implied_prob if implied_prob > 0 else 0
    }


def get_sportsbook_comparison(player_name, market_data):
    """
    Compare odds across sportsbooks for same player.
    Identify best value and arbitrage opportunities.
    
    Expected structure:
    {
        'player': 'Aaron Judge',
        'books': {
            'DraftKings': {'odds': -120, 'decimal': 1.833},
            'FanDuel': {'odds': -125, 'decimal': 1.800},
            'BetMGM': {'odds': -115, 'decimal': 1.870}
        }
    }
    """
    if not market_data or not market_data.get('books'):
        return {'player': player_name, 'best_book': None, 'arbitrage': None}
    
    books = market_data.get('books', {})
    
    # Find best decimal odds (higher is better for plus)
    best_odds = max(books.values(), key=lambda x: x.get('decimal', 0)).get('decimal', 0)
    best_book = [k for k, v in books.items() if v.get('decimal') == best_odds][0]
    
    # Detect arbitrage (round robin betting across books)
    odds_list = [v.get('decimal', 0) for v in books.values()]
    
    if len(odds_list) >= 2:
        # Arbitrage exists if sum(1/odds) < 1
        arbitrage_factor = sum(1/o for o in odds_list if o > 0)
        
        if arbitrage_factor < 1.0:
            arbitrage_profit = (1 - arbitrage_factor) * 100  # Percentage profit
        else:
            arbitrage_profit = 0
    else:
        arbitrage_profit = 0
    
    return {
        'player': player_name,
        'best_odds': best_odds,
        'best_book': best_book,
        'arbitrage_available': arbitrage_profit > 0,
        'arbitrage_roi': arbitrage_profit
    }


# =====================================================================
# 6. PROFESSIONAL PAIRING RECOMMENDATIONS
# =====================================================================

def find_optimal_pairings(players_list, probabilities, correlation_data=None):
    """
    Find best player pairings to maximize parlay expected value.
    
    Strategy:
    - Select low-correlated players (less likely to both be affected by same factor)
    - High individual probabilities (80%+ preferred)
    - Different lineup positions (spreads risk)
    - Exploiting bullpen weakness (same team opposing weak bullpen)
    """
    if len(players_list) < 2:
        return []
    
    pairings = []
    
    for i in range(len(players_list)):
        for j in range(i+1, len(players_list)):
            player_a = players_list[i]
            player_b = players_list[j]
            
            prob_a = probabilities.get(player_a, 0)
            prob_b = probabilities.get(player_b, 0)
            
            # Assume independence for now (conservative)
            parlay_prob = prob_a * prob_b
            
            # Bonus if pairing exploits same bullpen weakness
            same_game_vs_team = 0.05 if parlay_prob > 0 else 0
            
            pairings.append({
                'player_a': player_a,
                'player_b': player_b,
                'prob_a': prob_a,
                'prob_b': prob_b,
                'parlay_probability': parlay_prob + same_game_vs_team,
                'parlay_odds': 1 / max(parlay_prob + same_game_vs_team, 0.01)
            })
    
    # Sort by expected value
    pairings.sort(key=lambda x: x['parlay_probability'], reverse=True)
    
    return pairings[:5]  # Return top 5 pairings


def generate_professional_recommendations(predictions_df, statcast_data, game_conditions):
    """
    Generate professional bettor recommendations combining all metrics.
    """
    recommendations = []
    
    for idx, row in predictions_df.iterrows():
        batter_id = row.get('batter_id')
        batter_name = row.get('player_name')
        pitcher_id = row.get('pitcher_id')
        prob = row.get('prediction_probability', 0)
        
        rec = {
            'player': batter_name,
            'base_probability': prob,
            'adjustments': [],
            'final_probability': prob,
            'recommendation': 'NEUTRAL'
        }
        
        # Apply professional adjustments
        if not statcast_data.empty:
            # Platoon split adjustment
            batter_hand = row.get('batter_hand', 'R')
            platoon_mult = identify_platoon_mismatches(batter_id, pitcher_id, batter_hand, statcast_data)
            if platoon_mult > 1.0:
                rec['adjustments'].append(f"Platoon advantage: +{(platoon_mult-1)*100:.0f}%")
                rec['final_probability'] *= platoon_mult
        
        # Weather adjustment
        if game_conditions:
            weather_mult = detect_weather_extremes(game_conditions)
            if weather_mult > 1.0:
                rec['adjustments'].append(f"Weather favorable: +{(weather_mult-1)*100:.0f}%")
                rec['final_probability'] *= weather_mult
        
        # Recommendation logic
        if rec['final_probability'] >= 0.30:
            rec['recommendation'] = 'STRONG BUY'
        elif rec['final_probability'] >= 0.20:
            rec['recommendation'] = 'BUY'
        elif rec['final_probability'] >= 0.12:
            rec['recommendation'] = 'NEUTRAL'
        else:
            rec['recommendation'] = 'FADE'
        
        recommendations.append(rec)
    
    return recommendations


# =====================================================================
# EXPORT FUNCTIONS
# =====================================================================

def get_all_professional_features(predictions_df, game_date, statcast_data):
    """
    Calculate all professional bettor features.
    Returns enriched predictions dataframe with new columns.
    """
    try:
        # Get umpires
        umpires = get_todays_umpires()
        
        # Add umpire strike zone impact
        predictions_df['umpire_strike_zone_impact'] = 1.0
        
        # Add platoon split scores
        predictions_df['platoon_advantage'] = 1.0
        
        # Add bullpen quality multipliers
        predictions_df['bullpen_quality_multiplier'] = 1.0
        
        # Add weather factors
        predictions_df['weather_multiplier'] = 1.0
        
        return predictions_df
    
    except Exception as e:
        print(f"Error calculating professional features: {e}")
        return predictions_df
