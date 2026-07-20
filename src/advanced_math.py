# src/advanced_math.py
import math

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
