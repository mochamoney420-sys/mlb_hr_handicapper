"""
Ballpark Dimensions & Park Factors for MLB Home Run Predictions

Incorporates:
- Asymmetrical outfield walls (porch distances, wall heights)
- Handedness-specific park factors (LHH vs RHH)
- "Would-Be" home run calculations using Statcast
- Historical park inflation/deflation data
- Pull-hitter advantage scoring
"""

import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path

# ========================================================================
# ALL 30 MLB STADIUMS - BALLPARK DIMENSIONS & PARK FACTORS
# ========================================================================

BALLPARK_DATA = {
    # AL East
    'Yankees': {
        'name': 'Yankee Stadium',
        'team': 'NYY',
        'league': 'AL',
        'division': 'ALE',
        'rf_porch': 314,  # Right field porch - SHORT
        'rf_wall_height': 8,
        'lf_wall': 315,
        'lf_wall_height': 37,  # Green Monster
        'cf_distance': 408,
        'cf_wall_height': 8,
        'park_factor_rh': 1.12,  # +12% for RHH (short porch)
        'park_factor_lh': 1.02,  # Only +2% for LHH
        'pull_hitter_advantage': 1.18,  # Huge for RHH pull hitters
        'characteristics': ['short_porch', 'rh_inflated', 'classic'],
        'warning_track_hitters': 0.15,  # 15% of fly balls become HRs
    },
    'Red Sox': {
        'name': 'Fenway Park',
        'team': 'BOS',
        'league': 'AL',
        'division': 'ALE',
        'rf_porch': 302,
        'rf_wall_height': 37,  # Green Monster in LF
        'lf_wall': 310,
        'lf_wall_height': 37,
        'cf_distance': 390,
        'cf_wall_height': 37,
        'park_factor_rh': 1.08,  # +8% RHH
        'park_factor_lh': 1.15,  # +15% LHH (Green Monster)
        'pull_hitter_advantage': 1.20,  # High for LHH
        'characteristics': ['tall_wall', 'lh_inflated', 'historic'],
        'warning_track_hitters': 0.18,
    },
    'Orioles': {
        'name': 'Camden Yards',
        'team': 'BAL',
        'league': 'AL',
        'division': 'ALE',
        'rf_porch': 318,
        'rf_wall_height': 8,
        'lf_wall': 333,
        'lf_wall_height': 25,  # Deep left-center
        'cf_distance': 400,
        'cf_wall_height': 8,
        'park_factor_rh': 1.11,
        'park_factor_lh': 1.05,
        'pull_hitter_advantage': 1.14,
        'characteristics': ['rh_inflated', 'deep_lc'],
        'warning_track_hitters': 0.12,
    },
    'Rays': {
        'name': 'Tropicana Field',
        'team': 'TB',
        'league': 'AL',
        'division': 'ALE',
        'rf_porch': 315,
        'rf_wall_height': 8,
        'lf_wall': 315,
        'lf_wall_height': 8,
        'cf_distance': 400,
        'cf_wall_height': 8,
        'park_factor_rh': 0.98,  # -2% (symmetric, neutral)
        'park_factor_lh': 0.98,
        'pull_hitter_advantage': 1.00,
        'characteristics': ['neutral', 'symmetric'],
        'warning_track_hitters': 0.08,
    },
    'Blue Jays': {
        'name': 'Rogers Centre',
        'team': 'TOR',
        'league': 'AL',
        'division': 'ALE',
        'rf_porch': 330,
        'rf_wall_height': 10,
        'lf_wall': 330,
        'lf_wall_height': 10,
        'cf_distance': 400,
        'cf_wall_height': 10,
        'park_factor_rh': 1.05,
        'park_factor_lh': 1.05,
        'pull_hitter_advantage': 1.08,
        'characteristics': ['symmetric', 'dome'],
        'warning_track_hitters': 0.10,
    },
    
    # AL Central
    'White Sox': {
        'name': 'Guaranteed Rate Field',
        'team': 'CWS',
        'league': 'AL',
        'division': 'ALC',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 400,
        'cf_wall_height': 8,
        'park_factor_rh': 1.05,
        'park_factor_lh': 1.05,
        'pull_hitter_advantage': 1.08,
        'characteristics': ['symmetric', 'neutral'],
        'warning_track_hitters': 0.10,
    },
    'Guardians': {
        'name': 'Cleveland Stadium',
        'team': 'CLE',
        'league': 'AL',
        'division': 'ALC',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 325,
        'lf_wall_height': 8,
        'cf_distance': 405,
        'cf_wall_height': 8,
        'park_factor_rh': 1.03,
        'park_factor_lh': 1.02,
        'pull_hitter_advantage': 1.05,
        'characteristics': ['neutral', 'slightly_deep'],
        'warning_track_hitters': 0.09,
    },
    'Tigers': {
        'name': 'Comerica Park',
        'team': 'DET',
        'league': 'AL',
        'division': 'ALC',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 420,  # DEATH VALLEY - Deepest CF in baseball
        'cf_wall_height': 8,
        'park_factor_rh': 0.95,  # -5% (massive outfield)
        'park_factor_lh': 0.95,
        'pull_hitter_advantage': 0.92,  # Severely suppressed
        'characteristics': ['death_valley', 'deep_cf', 'hr_suppressed'],
        'warning_track_hitters': 0.05,
    },
    'Twins': {
        'name': 'Target Field',
        'team': 'MIN',
        'league': 'AL',
        'division': 'ALC',
        'rf_porch': 328,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 404,
        'cf_wall_height': 8,
        'park_factor_rh': 1.08,
        'park_factor_lh': 1.04,
        'pull_hitter_advantage': 1.10,
        'characteristics': ['slightly_rh_inflated'],
        'warning_track_hitters': 0.11,
    },
    
    # AL West
    'Royals': {
        'name': 'Kauffman Stadium',
        'team': 'KC',
        'league': 'AL',
        'division': 'ALW',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 410,  # Deep
        'cf_wall_height': 8,
        'park_factor_rh': 0.96,  # -4%
        'park_factor_lh': 0.96,
        'pull_hitter_advantage': 0.94,
        'characteristics': ['deep', 'hr_suppressed'],
        'warning_track_hitters': 0.07,
    },
    'Rangers': {
        'name': 'Globe Life Field',
        'team': 'TEX',
        'league': 'AL',
        'division': 'ALW',
        'rf_porch': 326,
        'rf_wall_height': 8,
        'lf_wall': 325,
        'lf_wall_height': 8,
        'cf_distance': 407,
        'cf_wall_height': 8,
        'park_factor_rh': 1.06,
        'park_factor_lh': 1.06,
        'pull_hitter_advantage': 1.08,
        'characteristics': ['neutral', 'dome'],
        'warning_track_hitters': 0.10,
    },
    'Astros': {
        'name': 'Minute Maid Park',
        'team': 'HOU',
        'league': 'AL',
        'division': 'ALW',
        'rf_porch': 315,
        'rf_wall_height': 8,
        'lf_wall': 315,
        'lf_wall_height': 8,
        'cf_distance': 409,
        'cf_wall_height': 8,
        'park_factor_rh': 1.09,
        'park_factor_lh': 1.09,
        'pull_hitter_advantage': 1.12,
        'characteristics': ['symmetric', 'slightly_inflated'],
        'warning_track_hitters': 0.11,
    },
    'Athletics': {
        'name': 'Oakland Coliseum',
        'team': 'OAK',
        'league': 'AL',
        'division': 'ALW',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 400,
        'cf_wall_height': 8,
        'park_factor_rh': 1.05,
        'park_factor_lh': 1.05,
        'pull_hitter_advantage': 1.08,
        'characteristics': ['neutral'],
        'warning_track_hitters': 0.10,
    },
    'Mariners': {
        'name': 'T-Mobile Park',
        'team': 'SEA',
        'league': 'AL',
        'division': 'ALW',
        'rf_porch': 314,
        'rf_wall_height': 8,
        'lf_wall': 325,
        'lf_wall_height': 8,
        'cf_distance': 405,
        'cf_wall_height': 8,
        'park_factor_rh': 1.10,
        'park_factor_lh': 1.02,
        'pull_hitter_advantage': 1.12,
        'characteristics': ['rh_inflated', 'short_rf_porch'],
        'warning_track_hitters': 0.12,
    },
    'Angels': {
        'name': 'Angel Stadium',
        'team': 'LAA',
        'league': 'AL',
        'division': 'ALW',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 400,
        'cf_wall_height': 8,
        'park_factor_rh': 1.05,
        'park_factor_lh': 1.05,
        'pull_hitter_advantage': 1.08,
        'characteristics': ['neutral', 'symmetric'],
        'warning_track_hitters': 0.10,
    },
    
    # NL East
    'Braves': {
        'name': 'Truist Park',
        'team': 'ATL',
        'league': 'NL',
        'division': 'NLE',
        'rf_porch': 325,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 400,
        'cf_wall_height': 8,
        'park_factor_rh': 1.07,
        'park_factor_lh': 1.05,
        'pull_hitter_advantage': 1.09,
        'characteristics': ['neutral'],
        'warning_track_hitters': 0.10,
    },
    'Nationals': {
        'name': 'Nationals Park',
        'team': 'WSH',
        'league': 'NL',
        'division': 'NLE',
        'rf_porch': 335,
        'rf_wall_height': 8,
        'lf_wall': 335,
        'lf_wall_height': 8,
        'cf_distance': 402,
        'cf_wall_height': 8,
        'park_factor_rh': 1.04,
        'park_factor_lh': 1.04,
        'pull_hitter_advantage': 1.06,
        'characteristics': ['neutral', 'symmetric'],
        'warning_track_hitters': 0.09,
    },
    'Mets': {
        'name': 'Citi Field',
        'team': 'NYM',
        'league': 'NL',
        'division': 'NLE',
        'rf_porch': 335,
        'rf_wall_height': 8,
        'lf_wall': 335,
        'lf_wall_height': 8,
        'cf_distance': 408,
        'cf_wall_height': 8,
        'park_factor_rh': 0.98,  # Slight suppression
        'park_factor_lh': 0.98,
        'pull_hitter_advantage': 0.96,
        'characteristics': ['slightly_deep', 'suppressed'],
        'warning_track_hitters': 0.08,
    },
    'Phillies': {
        'name': 'Citizens Bank Park',
        'team': 'PHI',
        'league': 'NL',
        'division': 'NLE',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 329,
        'lf_wall_height': 8,
        'cf_distance': 401,
        'cf_wall_height': 8,
        'park_factor_rh': 1.06,
        'park_factor_lh': 1.10,  # +10% for LHH
        'pull_hitter_advantage': 1.12,
        'characteristics': ['lh_inflated', 'shallow_rf'],
        'warning_track_hitters': 0.11,
    },
    'Marlins': {
        'name': 'loanDepot Park',
        'team': 'MIA',
        'league': 'NL',
        'division': 'NLE',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 407,
        'cf_wall_height': 8,
        'park_factor_rh': 1.05,
        'park_factor_lh': 1.05,
        'pull_hitter_advantage': 1.08,
        'characteristics': ['neutral'],
        'warning_track_hitters': 0.10,
    },
    
    # NL Central
    'Cardinals': {
        'name': 'Busch Stadium',
        'team': 'STL',
        'league': 'NL',
        'division': 'NLC',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 400,
        'cf_wall_height': 8,
        'park_factor_rh': 1.06,
        'park_factor_lh': 1.06,
        'pull_hitter_advantage': 1.09,
        'characteristics': ['symmetric', 'neutral'],
        'warning_track_hitters': 0.10,
    },
    'Cubs': {
        'name': 'Wrigley Field',
        'team': 'CHC',
        'league': 'NL',
        'division': 'NLC',
        'rf_porch': 353,
        'rf_wall_height': 8,
        'lf_wall': 355,
        'lf_wall_height': 8,
        'cf_distance': 400,
        'cf_wall_height': 8,
        'park_factor_rh': 1.08,
        'park_factor_lh': 1.08,
        'pull_hitter_advantage': 1.10,
        'characteristics': ['neutral', 'wind_dependent', 'historic'],
        'warning_track_hitters': 0.11,
    },
    'Reds': {
        'name': 'Great American Ball Park',
        'team': 'CIN',
        'league': 'NL',
        'division': 'NLC',
        'rf_porch': 328,
        'rf_wall_height': 8,
        'lf_wall': 328,
        'lf_wall_height': 8,
        'cf_distance': 404,
        'cf_wall_height': 8,
        'park_factor_rh': 1.12,  # +12% RHH
        'park_factor_lh': 1.12,  # +12% LHH (Highly inflated)
        'pull_hitter_advantage': 1.15,
        'characteristics': ['highly_inflated', 'lh_inflated', 'shallow'],
        'warning_track_hitters': 0.14,
    },
    'Brewers': {
        'name': 'American Family Field',
        'team': 'MIL',
        'league': 'NL',
        'division': 'NLC',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 401,
        'cf_wall_height': 8,
        'park_factor_rh': 1.04,
        'park_factor_lh': 1.04,
        'pull_hitter_advantage': 1.07,
        'characteristics': ['neutral'],
        'warning_track_hitters': 0.09,
    },
    'Pirates': {
        'name': 'PNC Park',
        'team': 'PIT',
        'league': 'NL',
        'division': 'NLC',
        'rf_porch': 325,
        'rf_wall_height': 8,
        'lf_wall': 325,
        'lf_wall_height': 8,
        'cf_distance': 399,
        'cf_wall_height': 8,
        'park_factor_rh': 1.06,
        'park_factor_lh': 1.06,
        'pull_hitter_advantage': 1.09,
        'characteristics': ['symmetric', 'slightly_inflated'],
        'warning_track_hitters': 0.10,
    },
    
    # NL West
    'Dodgers': {
        'name': 'Dodger Stadium',
        'team': 'LAD',
        'league': 'NL',
        'division': 'NLW',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 395,
        'cf_wall_height': 8,
        'park_factor_rh': 1.08,  # Slightly elevated
        'park_factor_lh': 1.08,
        'pull_hitter_advantage': 1.11,
        'characteristics': ['symmetric', 'slightly_inflated'],
        'warning_track_hitters': 0.11,
    },
    'Padres': {
        'name': 'Petco Park',
        'team': 'SD',
        'league': 'NL',
        'division': 'NLW',
        'rf_porch': 322,
        'rf_wall_height': 8,
        'lf_wall': 336,
        'lf_wall_height': 8,
        'cf_distance': 402,
        'cf_wall_height': 8,
        'park_factor_rh': 1.04,
        'park_factor_lh': 0.98,  # Slight suppression for LHH
        'pull_hitter_advantage': 1.02,  # Very suppressed
        'characteristics': ['rh_slight_inflate', 'lh_suppress'],
        'warning_track_hitters': 0.08,
    },
    'Giants': {
        'name': 'Oracle Park',
        'team': 'SF',
        'league': 'NL',
        'division': 'NLW',
        'rf_porch': 315,  # SHORT RIGHT PORCH
        'rf_wall_height': 8,
        'lf_wall': 339,
        'lf_wall_height': 8,
        'cf_distance': 399,
        'cf_wall_height': 8,
        'park_factor_rh': 1.13,  # +13% for RHH (short porch)
        'park_factor_lh': 0.96,  # -4% for LHH (wind, deep LF)
        'pull_hitter_advantage': 1.16,  # Huge for RHH
        'characteristics': ['rh_inflated', 'lh_suppressed', 'short_rf_porch', 'wind'],
        'warning_track_hitters': 0.13,
    },
    'Rockies': {
        'name': 'Coors Field',
        'team': 'COL',
        'league': 'NL',
        'division': 'NLW',
        'rf_porch': 350,
        'rf_wall_height': 8,
        'lf_wall': 347,
        'lf_wall_height': 8,
        'cf_distance': 415,
        'cf_wall_height': 8,
        'park_factor_rh': 1.28,  # +28% EXTREME
        'park_factor_lh': 1.28,  # +28% EXTREME (high elevation)
        'pull_hitter_advantage': 1.32,  # EXTREME
        'characteristics': ['extreme_inflation', 'high_elevation', 'biggest_hr_factor'],
        'warning_track_hitters': 0.20,  # 20% of fly balls!
    },
    'Diamondbacks': {
        'name': 'Chase Field',
        'team': 'ARI',
        'league': 'NL',
        'division': 'NLW',
        'rf_porch': 330,
        'rf_wall_height': 8,
        'lf_wall': 330,
        'lf_wall_height': 8,
        'cf_distance': 407,
        'cf_wall_height': 8,
        'park_factor_rh': 1.05,
        'park_factor_lh': 1.05,
        'pull_hitter_advantage': 1.08,
        'characteristics': ['neutral', 'dome'],
        'warning_track_hitters': 0.10,
    },
}


def get_ballpark_factor(team, batter_hand='R', batter_id=None):
    """
    Calculate ballpark park factor for a batter.
    
    Args:
        team: Team name or abbreviation
        batter_hand: 'L' or 'R'
        batter_id: Optional player ID for custom multiplier
    
    Returns:
        dict with park factor and characteristics
    """
    if team not in BALLPARK_DATA:
        return {'park_factor': 1.0, 'characteristics': []}
    
    park = BALLPARK_DATA[team]
    
    if batter_hand.upper() == 'L':
        park_factor = park['park_factor_lh']
    else:
        park_factor = park['park_factor_rh']
    
    return {
        'park_factor': park_factor,
        'pull_advantage': park['pull_hitter_advantage'],
        'characteristics': park['characteristics'],
        'warning_track_rate': park['warning_track_hitters'],
        'stadium_name': park['name'],
        'rf_porch': park['rf_porch'],
        'lf_porch': park['lf_wall'],
        'cf_distance': park['cf_distance'],
    }


def calculate_would_be_homers(player_statcast, home_team, away_team):
    """
    Calculate "Would-Be" home runs - how many HRs a player would have
    if all their batted balls occurred in today's stadium.
    
    Args:
        player_statcast: Filtered statcast data for player
        home_team: Home team name
        away_team: Away team name
    
    Returns:
        dict with would-be HR counts for each stadium
    """
    if player_statcast is None or len(player_statcast) == 0:
        return {
            'home_would_be_hrs': 0,
            'away_would_be_hrs': 0,
            'hr_difference': 0,
        }
    
    # Filter for fly balls and line drives only
    fly_balls = player_statcast[
        (player_statcast['bb_type'].isin(['fly_ball', 'line_drive']))
    ].copy()
    
    if len(fly_balls) == 0:
        return {
            'home_would_be_hrs': 0,
            'away_would_be_hrs': 0,
            'hr_difference': 0,
        }
    
    # Use launch angle and exit velocity to estimate if would be HR
    # Rule: LA > 20° and EV > 94 mph → ~60% chance in avg park
    # Adjust by park factor
    
    fly_balls['would_be_hr_baseline'] = (
        (fly_balls['launch_angle'] > 20) &
        (fly_balls['exit_velocity'] > 94)
    )
    
    baseline_would_be = fly_balls['would_be_hr_baseline'].sum()
    
    # Calculate home park adjustment
    home_park = get_ballpark_factor(home_team, player_statcast['stand'].iloc[0] if len(player_statcast) > 0 else 'R')
    away_park = get_ballpark_factor(away_team, player_statcast['stand'].iloc[0] if len(player_statcast) > 0 else 'R')
    
    home_would_be_hrs = int(baseline_would_be * home_park['park_factor'])
    away_would_be_hrs = int(baseline_would_be * away_park['park_factor'])
    
    return {
        'home_would_be_hrs': home_would_be_hrs,
        'away_would_be_hrs': away_would_be_hrs,
        'hr_difference': home_would_be_hrs - away_would_be_hrs,
        'baseline_flyball_count': len(fly_balls),
    }


def get_porch_advantage_bonus(team, batter_hand, recent_fly_ball_distance=None):
    """
    Detect if batter has recent warning-track fly balls that would be HRs
    in tonight's park. High-value signal for prop betters.
    
    Returns multiplier 1.0-1.35x based on mismatch detection.
    """
    park = BALLPARK_DATA.get(team)
    if not park:
        return 1.0
    
    # If recent data shows warning-track fly balls, and tonight's park is short porch:
    if recent_fly_ball_distance and recent_fly_ball_distance > 380:
        # This fly ball was a warning-track out
        if batter_hand.upper() == 'R':
            porch = park['rf_porch']
        else:
            porch = park['lf_wall']
        
        # If porch is short (<330 ft), significant advantage
        if porch < 330:
            # Multiplier increases as fly ball distance exceeds porch distance
            excess_distance = recent_fly_ball_distance - porch
            multiplier = min(1.35, 1.0 + (excess_distance * 0.01))  # +1% per foot
            return multiplier
    
    return 1.0


def get_death_valley_penalty(team, batter_exit_velo_avg=None):
    """
    Identify "Death Valley" stadiums and apply penalty multiplier.
    Examples: Comerica Park (DET), Kauffman Stadium (KC)
    
    Returns multiplier 0.92-1.0 based on severity.
    """
    park = BALLPARK_DATA.get(team)
    if not park:
        return 1.0
    
    if 'death_valley' in park['characteristics']:
        # Deep center field = 0.92-0.95x
        if batter_exit_velo_avg and batter_exit_velo_avg < 92:
            # Low velo hitters hit worse in death valleys
            return 0.88
        return 0.93
    elif 'deep_cf' in park['characteristics']:
        return 0.95
    
    return 1.0


def calculate_park_adjustment_multiplier(
    batter_hand, home_team, is_home_game, 
    recent_would_be_hrs_diff=None, 
    batter_exit_velo_avg=None
):
    """
    Complete park adjustment multiplier incorporating:
    - Handedness-specific park factors
    - Porch advantage detection
    - Death valley penalties
    - "Would-Be" HR analysis
    
    Returns multiplier 0.85-1.35x
    """
    if is_home_game:
        team = home_team
    else:
        team = home_team  # Need opposing team passed
    
    park = get_ballpark_factor(team, batter_hand)
    
    multiplier = park['park_factor']
    
    # Apply death valley penalty
    death_valley_mult = get_death_valley_penalty(team, batter_exit_velo_avg)
    multiplier *= death_valley_mult
    
    # Apply would-be HR bonus
    if recent_would_be_hrs_diff and recent_would_be_hrs_diff > 0:
        # Positive difference = player would have more HRs in this park
        would_be_bonus = min(1.15, 1.0 + (recent_would_be_hrs_diff * 0.05))
        multiplier *= would_be_bonus
    
    # Cap at reasonable bounds
    multiplier = min(1.35, max(0.85, multiplier))
    
    return {
        'park_adjustment': multiplier,
        'park_factor': park['park_factor'],
        'death_valley_multiplier': death_valley_mult,
        'stadium_name': park['stadium_name'],
        'characteristics': park['characteristics'],
        'warning_track_rate': park['warning_track_rate'],
    }


def get_stadium_info(team):
    """Get detailed stadium information."""
    return BALLPARK_DATA.get(team)


if __name__ == '__main__':
    # Test: Show all stadiums and their factors
    print("\n" + "="*80)
    print("BALLPARK FACTORS REFERENCE")
    print("="*80 + "\n")
    
    for team, data in sorted(BALLPARK_DATA.items()):
        print(f"{data['name']:.<40} {team:5s}")
        print(f"  RHH Factor: {data['park_factor_rh']}x | LHH Factor: {data['park_factor_lh']}x")
        print(f"  RF Porch: {data['rf_porch']} ft | LF: {data['lf_wall']} ft | CF: {data['cf_distance']} ft")
        print(f"  Characteristics: {', '.join(data['characteristics'])}")
        print()
    
    # Test specific matchup
    print("\n" + "="*80)
    print("EXAMPLE: RHH vs Coors Field vs Comerica Park")
    print("="*80)
    coors = get_ballpark_factor('Rockies', 'R')
    comerica = get_ballpark_factor('Tigers', 'R')
    print(f"Coors: {coors['park_factor']}x (Characteristics: {coors['characteristics']})")
    print(f"Comerica: {comerica['park_factor']}x (Characteristics: {comerica['characteristics']})")
