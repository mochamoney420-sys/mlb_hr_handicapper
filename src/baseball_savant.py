#!/usr/bin/env python
"""
Baseball Savant Integration: Fetch game details, lineups, and batted ball data.
Checks lineups every morning and again before games start.
"""

import os
import json
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd
import statsapi
import time

# =====================================================================
# GAME DETAILS & LINEUPS FROM BASEBALL SAVANT / STATSAPI
# =====================================================================

def get_todays_games():
    """Fetch all today's MLB games from StatsAPI."""
    try:
        today = datetime.today().strftime('%Y-%m-%d')
        games = statsapi.schedule(start_date=today, end_date=today)
        return games if games else []
    except Exception as e:
        print(f"Error fetching today's games: {e}")
        return []


def get_game_lineups(game_id):
    """Fetch complete lineup for a specific game from StatsAPI."""
    try:
        game_data = statsapi.get('game', {'gamePk': game_id})
        lineups = {
            'away_team': game_data.get('gameData', {}).get('teams', {}).get('away', {}).get('name', 'Unknown'),
            'home_team': game_data.get('gameData', {}).get('teams', {}).get('home', {}).get('name', 'Unknown'),
            'away_players': [],
            'home_players': []
        }
        
        # Try to get batting orders from the official batting order first
        away_batting_order = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get('away', {}).get('batters', [])
        home_batting_order = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get('home', {}).get('batters', [])
        
        def _build_ordered_lineup(team_key, batting_order_ids):
            players = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get(team_key, {}).get('players', {})
            ordered = []

            ordered_ids = [str(pid).replace('ID', '') for pid in (batting_order_ids or [])]
            if not ordered_ids:
                # Conservative fallback: use only non-pitchers from current boxscore roster, capped to 9.
                ordered_ids = [
                    str(pid).replace('ID', '') for pid, pdata in players.items()
                    if pdata.get('position', {}).get('abbreviation') not in ['P', 'UNK']
                ][:9]

            seen = set()
            for slot, batter_id in enumerate(ordered_ids, 1):
                batter_id = str(batter_id).replace('ID', '')
                if not batter_id or batter_id in seen:
                    continue
                seen.add(batter_id)
                player_data = players.get(f'ID{batter_id}', {})
                player_info = player_data.get('person', {})
                ordered.append({
                    'id': f'ID{batter_id}',
                    'name': player_info.get('fullName', f'Unknown (#{batter_id})'),
                    'number': player_data.get('jerseyNumber', 0),
                    'position': player_data.get('position', {}).get('abbreviation', 'UNK'),
                    'is_batter': True,
                    'batting_order_slot': slot,
                })
                if len(ordered) >= 9:
                    break
            return ordered

        lineups['away_players'] = _build_ordered_lineup('away', away_batting_order)
        lineups['home_players'] = _build_ordered_lineup('home', home_batting_order)
        
        return lineups
    except Exception as e:
        print(f"Error fetching lineups for game {game_id}: {e}")
        return None


def get_batting_orders_for_games():
    """Fetch batting orders for all today's games."""
    games = get_todays_games()
    if not games:
        print("No games today.")
        return {}
    
    batting_orders = {}
    for game in games:
        game_id = game['game_id']
        away_team = game['away_name']
        home_team = game['home_name']
        
        try:
            game_data = statsapi.get('game', {'gamePk': game_id})
            
            # Get away team batting order - try multiple sources
            away_order = []
            # First try the batters array in boxscore
            away_batters = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get('away', {}).get('batters', [])
            # If empty, try to get all non-pitcher players from players dict
            if not away_batters:
                away_players = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get('away', {}).get('players', {})
                away_batters = [pid.replace('ID', '') for pid, pdata in away_players.items() 
                               if pdata.get('position', {}).get('abbreviation') != 'P']
            
            for idx, batter_id in enumerate(away_batters[:9], 1):  # Max 9 batters
                try:
                    batter_key = f'ID{batter_id}' if not str(batter_id).startswith('ID') else batter_id
                    batter_data = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get('away', {}).get('players', {}).get(batter_key, {})
                    batter_name = batter_data.get('person', {}).get('fullName', f'Unknown (#{batter_id})')
                    away_order.append({'slot': idx, 'player_id': batter_id, 'name': batter_name})
                except Exception:
                    pass
            
            # Get home team batting order - try multiple sources
            home_order = []
            # First try the batters array in boxscore
            home_batters = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get('home', {}).get('batters', [])
            # If empty, try to get all non-pitcher players from players dict
            if not home_batters:
                home_players = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get('home', {}).get('players', {})
                home_batters = [pid.replace('ID', '') for pid, pdata in home_players.items() 
                               if pdata.get('position', {}).get('abbreviation') != 'P']
            
            for idx, batter_id in enumerate(home_batters[:9], 1):  # Max 9 batters
                try:
                    batter_key = f'ID{batter_id}' if not str(batter_id).startswith('ID') else batter_id
                    batter_data = game_data.get('liveData', {}).get('boxscore', {}).get('teams', {}).get('home', {}).get('players', {}).get(batter_key, {})
                    batter_name = batter_data.get('person', {}).get('fullName', f'Unknown (#{batter_id})')
                    home_order.append({'slot': idx, 'player_id': batter_id, 'name': batter_name})
                except Exception:
                    pass
            
            batting_orders[game_id] = {
                'away_team': away_team,
                'home_team': home_team,
                'away_batting_order': away_order,
                'home_batting_order': home_order,
                'first_pitch': game.get('game_datetime', 'Unknown')
            }
        except Exception as e:
            print(f"Error getting batting order for game {game_id}: {e}")
    
    return batting_orders


def get_live_game_status(game_id):
    """Get current status and score of a game."""
    try:
        game_data = statsapi.get('game', {'gamePk': game_id})
        status = {
            'game_id': game_id,
            'status': game_data.get('gameData', {}).get('status', {}).get('detailedState', 'Unknown'),
            'inning': game_data.get('liveData', {}).get('linescore', {}).get('currentInning', 0),
            'away_score': game_data.get('liveData', {}).get('linescore', {}).get('teams', {}).get('away', {}).get('runs', 0),
            'home_score': game_data.get('liveData', {}).get('linescore', {}).get('teams', {}).get('home', {}).get('runs', 0)
        }
        return status
    except Exception as e:
        print(f"Error fetching game status: {e}")
        return None


def check_lineups_morning():
    """Morning check: Verify all lineups are available (typically 9-10 AM ET)."""
    print("\n" + "="*70)
    print("🌅 MORNING LINEUP CHECK — Verifying all today's lineups")
    print("="*70)
    
    games = get_todays_games()
    if not games:
        print("No games scheduled for today.")
        return {}
    
    print(f"Found {len(games)} games today.\n")
    
    lineups_dict = {}
    for game in games:
        game_id = game['game_id']
        away_name = game['away_name']
        home_name = game['home_name']
        first_pitch = game.get('game_datetime', 'Unknown')
        
        print(f"  Game {game_id}: {away_name} @ {home_name}")
        print(f"    First pitch: {first_pitch}")
        
        lineups = get_game_lineups(game_id)
        if lineups:
            away_batters = [p['name'] for p in lineups['away_players'] if p['is_batter']]
            home_batters = [p['name'] for p in lineups['home_players'] if p['is_batter']]
            
            print(f"    Away lineup: {len(away_batters)} batters confirmed")
            print(f"    Home lineup: {len(home_batters)} batters confirmed")
            
            lineups_dict[game_id] = lineups
        else:
            print(f"    ⚠️  Lineup not yet available")
        
        time.sleep(0.5)  # Rate limiting
    
    return lineups_dict


def check_lineups_pregame():
    """Pre-game check (2-3 hours before first pitch): Verify final lineups."""
    print("\n" + "="*70)
    print("⚾ PRE-GAME LINEUP CHECK — Confirming final lineups before games")
    print("="*70)
    
    games = get_todays_games()
    if not games:
        print("No games found.")
        return {}
    
    now = datetime.now()
    lineups_dict = {}
    changes = []
    
    for game in games:
        game_id = game['game_id']
        away_name = game['away_name']
        home_name = game['home_name']
        first_pitch = game.get('game_datetime', 'Unknown')
        
        # Parse first pitch time to check if game is coming up
        try:
            fp_time = datetime.fromisoformat(first_pitch.replace('Z', '+00:00'))
            time_to_first_pitch = (fp_time - now).total_seconds() / 3600
            
            if time_to_first_pitch > 6 or time_to_first_pitch < 0:
                continue  # Skip if not within 6 hours or already started
        except:
            pass
        
        print(f"\n  Game {game_id}: {away_name} @ {home_name}")
        
        lineups = get_game_lineups(game_id)
        if lineups:
            away_batters = [p for p in lineups['away_players'] if p['is_batter']]
            home_batters = [p for p in lineups['home_players'] if p['is_batter']]
            
            print(f"    ✓ Away lineup: {len(away_batters)} batters")
            print(f"    ✓ Home lineup: {len(home_batters)} batters")
            
            # Check for late scratches
            for i, batter in enumerate(away_batters[:9], 1):
                print(f"      {i}. {batter['name']} ({batter['position']})")
            
            lineups_dict[game_id] = lineups
        else:
            print(f"    ⚠️  Error fetching lineups")
        
        time.sleep(0.5)
    
    return lineups_dict


def save_lineup_report(lineups_dict, timestamp_label=""):
    """Save lineup verification report to file."""
    data_dir = Path("data")
    data_dir.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y-%m-%d")
    filename = data_dir / f"lineup_report_{timestamp}{timestamp_label}.json"
    
    report = {
        'timestamp': datetime.now().isoformat(),
        'games': lineups_dict
    }
    
    with open(filename, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    
    print(f"\n✓ Lineup report saved: {filename}")
    return filename


def get_batted_balls_quality_metrics(statcast_data):
    """
    Extract and analyze batted ball quality metrics from Statcast data.
    Returns metrics on barrel rates, sweet spot, exit velo, etc.
    """
    if statcast_data is None or statcast_data.empty:
        return {}
    
    # Filter to only batted balls (in_play events)
    batted_balls = statcast_data[statcast_data['type'] == 'pitch'].copy()
    batted_balls = batted_balls[batted_balls['events'].notna()]
    
    metrics = {
        'total_batted_balls': len(batted_balls),
        'total_fly_balls': len(batted_balls[batted_balls['bb_type'] == 'fly_ball']),
        'total_ground_balls': len(batted_balls[batted_balls['bb_type'] == 'ground_ball']),
        'total_line_drives': len(batted_balls[batted_balls['bb_type'] == 'line_drive']),
        'total_popups': len(batted_balls[batted_balls['bb_type'] == 'popup']),
        
        'avg_exit_velocity': batted_balls['launch_speed'].mean(),
        'median_exit_velocity': batted_balls['launch_speed'].median(),
        'max_exit_velocity': batted_balls['launch_speed'].max(),
        
        'avg_launch_angle': batted_balls['launch_angle'].mean(),
        'barrel_count': len(batted_balls[(batted_balls['launch_speed'] >= 98) & 
                                          (batted_balls['launch_angle'].between(26, 30))]),
        'sweet_spot_count': len(batted_balls[(batted_balls['launch_speed'] >= 90) & 
                                              (batted_balls['launch_angle'].between(18, 32))]),
        'hard_hit_count': len(batted_balls[batted_balls['launch_speed'] >= 95]),
        
        'home_runs': len(batted_balls[batted_balls['events'] == 'home_run']),
        'extra_base_hits': len(batted_balls[batted_balls['events'].isin(['double', 'triple', 'home_run'])]),
    }
    
    # Calculate rates
    if metrics['total_batted_balls'] > 0:
        metrics['barrel_rate'] = metrics['barrel_count'] / metrics['total_batted_balls']
        metrics['sweet_spot_rate'] = metrics['sweet_spot_count'] / metrics['total_batted_balls']
        metrics['hard_hit_rate'] = metrics['hard_hit_count'] / metrics['total_batted_balls']
        metrics['fly_ball_rate'] = metrics['total_fly_balls'] / metrics['total_batted_balls']
        metrics['ground_ball_rate'] = metrics['total_ground_balls'] / metrics['total_batted_balls']
        metrics['hr_per_fly_ball'] = metrics['home_runs'] / max(metrics['total_fly_balls'], 1)
    
    return metrics


def get_batter_batted_balls_profile(statcast_data, batter_id):
    """Get batted ball profile for specific batter."""
    if statcast_data is None or statcast_data.empty:
        return {}
    
    batter_data = statcast_data[
        (statcast_data['batter'] == batter_id) & 
        (statcast_data['type'] == 'pitch') &
        (statcast_data['events'].notna())
    ].copy()
    
    if batter_data.empty:
        return {}
    
    profile = {
        'batter_id': batter_id,
        'total_batted_balls': len(batter_data),
        'avg_exit_velocity': batter_data['launch_speed'].mean(),
        'barrel_rate': len(batter_data[(batter_data['launch_speed'] >= 98) & 
                                        (batter_data['launch_angle'].between(26, 30))]) / len(batter_data),
        'sweet_spot_rate': len(batter_data[(batter_data['launch_speed'] >= 90) & 
                                            (batter_data['launch_angle'].between(18, 32))]) / len(batter_data),
        'hard_hit_rate': len(batter_data[batter_data['launch_speed'] >= 95]) / len(batter_data),
        'fly_ball_rate': len(batter_data[batter_data['bb_type'] == 'fly_ball']) / len(batter_data),
        'home_runs': len(batter_data[batter_data['events'] == 'home_run']),
        'hr_per_fly_ball': len(batter_data[batter_data['events'] == 'home_run']) / max(
            len(batter_data[batter_data['bb_type'] == 'fly_ball']), 1
        )
    }
    
    return profile


def get_pitcher_batted_balls_allowed(statcast_data, pitcher_id):
    """Get batted balls allowed profile for specific pitcher."""
    if statcast_data is None or statcast_data.empty:
        return {}
    
    pitcher_data = statcast_data[
        (statcast_data['pitcher'] == pitcher_id) & 
        (statcast_data['type'] == 'pitch') &
        (statcast_data['events'].notna())
    ].copy()
    
    if pitcher_data.empty:
        return {}
    
    profile = {
        'pitcher_id': pitcher_id,
        'total_batted_balls_allowed': len(pitcher_data),
        'avg_exit_velocity_allowed': pitcher_data['launch_speed'].mean(),
        'barrel_rate_allowed': len(pitcher_data[(pitcher_data['launch_speed'] >= 98) & 
                                                  (pitcher_data['launch_angle'].between(26, 30))]) / len(pitcher_data),
        'hard_hit_rate_allowed': len(pitcher_data[pitcher_data['launch_speed'] >= 95]) / len(pitcher_data),
        'fly_ball_rate_allowed': len(pitcher_data[pitcher_data['bb_type'] == 'fly_ball']) / len(pitcher_data),
        'home_runs_allowed': len(pitcher_data[pitcher_data['events'] == 'home_run']),
        'hr_per_fly_ball_allowed': len(pitcher_data[pitcher_data['events'] == 'home_run']) / max(
            len(pitcher_data[pitcher_data['bb_type'] == 'fly_ball']), 1
        )
    }
    
    return profile


def print_lineup_summary(lineups_dict):
    """Print readable summary of all lineups."""
    print("\n" + "="*70)
    print("LINEUP SUMMARY")
    print("="*70)
    
    for game_id, lineups in lineups_dict.items():
        print(f"\n{lineups['away_team']} @ {lineups['home_team']}")
        print("-" * 70)
        
        away_batters = sorted([p for p in lineups['away_players'] if p['is_batter']], 
                             key=lambda x: x.get('order', 99))
        home_batters = sorted([p for p in lineups['home_players'] if p['is_batter']], 
                             key=lambda x: x.get('order', 99))
        
        print(f"Away ({len(away_batters)} batters):")
        for batter in away_batters[:9]:
            print(f"  {batter['name']:<30} {batter['position']}")
        
        print(f"\nHome ({len(home_batters)} batters):")
        for batter in home_batters[:9]:
            print(f"  {batter['name']:<30} {batter['position']}")


if __name__ == "__main__":
    # Test functions
    print("Baseball Savant Integration Module\n")
    
    print("Testing morning lineup check...")
    morning_lineups = check_lineups_morning()
    
    print("\n\nTesting pre-game lineup check...")
    pregame_lineups = check_lineups_pregame()
    
    if morning_lineups:
        print_lineup_summary(morning_lineups)
        save_lineup_report(morning_lineups, "_morning")
