# src/advanced_math.py
import math

try:
    import pandas as pd
except ImportError:  # pragma: no cover - fallback for minimal environments
    pd = None


def calculate_log5_matchup(hr_pa_batter, hr_pa_pitcher, hr_pa_league=0.031):
    """
    Blends batter and pitcher platoon-specific HR/PA baselines using 
    the Log-5 Odds Ratio method against a standard league-wide baseline.
    """
    # Prevent math domain divisions by zero
    if hr_pa_league <= 0 or hr_pa_league >= 1:
        return 0.0
        
    numerator = (hr_pa_batter * hr_pa_pitcher) / hr_pa_league
    
    denominator_part_1 = numerator
    denominator_part_2 = ((1.0 - hr_pa_batter) * (1.0 - hr_pa_pitcher)) / (1.0 - hr_pa_league)
    
    denominator = denominator_part_1 + denominator_part_2
    
    if denominator == 0:
        return 0.0
        
    return numerator / denominator


def apply_environmental_factors(base_prob, park_factor_hr, temperature, wind_speed, wind_direction):
    """
    Applies multiplicative park adjustments and temperature/wind density scaling.
    """
    # 1. Apply rolling park factor scaling
    adjusted_prob = base_prob * park_factor_hr
    
    # 2. Temperature adjustment: +2.5% advantage per 10°F above 70°F
    if temperature > 70.0:
        temp_delta = (temperature - 70.0) / 10.0
        adjusted_prob *= (1.0 + (0.025 * temp_delta))
        
    # 3. Wind drag adjustment: Suppress if blowing inward heavily
    if wind_speed > 10.0 and wind_direction.lower() == "inward":
        adjusted_prob *= 0.88
        
    return adjusted_prob


def calculate_poisson_prop_prob(adjusted_hr_pa, projected_pa=4.1):
    """
    Converts individual plate-appearance rates into a full game prop probability
    using a standard Poisson distribution.
    """
    lambda_val = adjusted_hr_pa * projected_pa
    
    # Probability of hitting exactly 0 HRs
    prob_zero_hr = math.exp(-lambda_val)
    
    # Probability of hitting 1 or more HRs
    final_prop_probability = 1.0 - prob_zero_hr
    return final_prop_probability


def _cluster_rate_from_frame(cluster_df, *, batter_id=None, pitcher_id=None, batter_hand=None,
                             pitcher_hand=None, min_pa=500, fallback=None):
    """Compute a cluster-level HR/PA rate from local aggregated data."""
    if pd is None:
        raise ImportError("pandas is required for cluster-based calculations")

    if cluster_df is None or not isinstance(cluster_df, pd.DataFrame):
        raise TypeError("cluster_df must be a pandas DataFrame")

    mask = True
    if batter_id is not None:
        mask &= (cluster_df['batter_id'] == batter_id)
    if pitcher_id is not None:
        mask &= (cluster_df['pitcher_id'] == pitcher_id)
    if batter_hand is not None:
        mask &= (cluster_df['batter_hand'] == batter_hand)
    if pitcher_hand is not None:
        mask &= (cluster_df['pitcher_hand'] == pitcher_hand)

    subset = cluster_df.loc[mask]
    pa_total = int(subset['pa'].sum()) if 'pa' in subset.columns else 0
    hr_total = int(subset['hr'].sum()) if 'hr' in subset.columns else 0

    if pa_total >= min_pa and pa_total > 0:
        return hr_total / pa_total

    if fallback is not None:
        return fallback

    return None


def calculate_platoon_cluster_prob(cluster_df, *, batter_id, pitcher_id, batter_hand,
                                   pitcher_hand, league_hr_pa=0.031, min_pa=500,
                                   projected_pa=4.1, park_factor_hr=1.0,
                                   temperature=70.0, wind_speed=0.0,
                                   wind_direction='neutral'):
    """
    Build a matchup probability using aggregated local cluster data and the Log-5 method.

    Expected cluster_df columns:
      batter_id, pitcher_id, batter_hand, pitcher_hand, pa, hr
    Optional columns for richer environmental handling:
      park_factor_hr, temperature, wind_speed, wind_direction
    """
    batter_rate = _cluster_rate_from_frame(
        cluster_df,
        batter_id=batter_id,
        batter_hand=batter_hand,
        pitcher_hand=pitcher_hand,
        min_pa=min_pa,
        fallback=None,
    )
    pitcher_rate = _cluster_rate_from_frame(
        cluster_df,
        pitcher_id=pitcher_id,
        batter_hand=batter_hand,
        pitcher_hand=pitcher_hand,
        min_pa=min_pa,
        fallback=None,
    )

    if batter_rate is None or pitcher_rate is None:
        # Fall back to a broad hand-based cluster when the exact matchup bucket is thin.
        batter_rate = _cluster_rate_from_frame(
            cluster_df,
            batter_id=batter_id,
            batter_hand=batter_hand,
            min_pa=max(100, min_pa // 2),
            fallback=league_hr_pa,
        )
        pitcher_rate = _cluster_rate_from_frame(
            cluster_df,
            pitcher_id=pitcher_id,
            pitcher_hand=pitcher_hand,
            min_pa=max(100, min_pa // 2),
            fallback=league_hr_pa,
        )

    base_prob = calculate_log5_matchup(batter_rate, pitcher_rate, hr_pa_league=league_hr_pa)
    adjusted_prob = apply_environmental_factors(
        base_prob,
        park_factor_hr=park_factor_hr,
        temperature=temperature,
        wind_speed=wind_speed,
        wind_direction=wind_direction,
    )
    return calculate_poisson_prop_prob(adjusted_prob, projected_pa=projected_pa)


def convert_prob_to_american_odds(probability):
    """
    Converts a raw decimal probability into standard American betting odds format.
    """
    if probability <= 0.0 or probability >= 1.0:
        return "+0"
        
    if probability >= 0.50:
        odds = int((probability / (1.0 - probability)) * -100)
        return f"{odds}"
    else:
        odds = int(((1.0 - probability) / probability) * 100)
        return f"+{odds}"
