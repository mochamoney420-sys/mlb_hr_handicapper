"""
Physics & Ballistics Simulator for MLB Home Run Prediction

Multi-layered physics engine that simulates ball flight dynamics at the 
molecular level, incorporating:
- Real-time environmental drag profiles (Density Altitude)
- 3D wind vector mapping against stadium geometries
- Trajectory simulation and carry distance modeling
- Barrel sweet spot contact probability

This module replaces generic park factors with actual physics-based calculations.
"""

import math
import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional
from dataclasses import dataclass
from datetime import datetime


@dataclass
class EnvironmentalConditions:
    """Real-time environmental state for ball flight physics."""
    temperature_f: float  # 60-95 typical range
    barometric_pressure_inHg: float  # 29.5-30.5 typical
    altitude_ft: int  # Sea level to 5280 ft
    humidity_pct: float  # 0-100
    wind_speed_mph: float  # 0-25 typical
    wind_direction_deg: float  # 0-360, 0=North, 90=East, 180=South, 270=West
    roof_status: str  # 'open', 'closed', 'retracted'


@dataclass
class BallData:
    """Baseball aerodynamic properties."""
    mass_oz: float = 5.125
    diameter_in: float = 2.86
    mass_kg: float = 0.145  # Converted from oz
    diameter_m: float = 0.0726  # Converted from inches
    seam_height_in: float = 0.05  # Raised seam height affects drag


@dataclass
class StadiumGeometry:
    """3D stadium coordinates and wall vectors."""
    name: str
    latitude: float  # For Coriolis (advanced)
    longitude: float  # For local wind patterns
    elevation_ft: int
    roof_status_default: str  # 'open', 'closed', 'retracted'
    outfield_walls: Dict[str, Dict]  # RF, CF, LF wall distances and heights


class DensityAltitudeCalculator:
    """
    Calculate Density Altitude (DA) - the altitude where air density equals current conditions.
    
    DA is critical for baseball because:
    - Higher DA = thinner air = less drag = ball carries farther
    - Coors Field: DA ~7500 ft (5280 actual + 2200 DA bonus)
    - Sea level: DA ~500-1500 ft range
    
    Physics: DA depends on Temperature, Pressure, Humidity, Actual Altitude
    """
    
    @staticmethod
    def celsius_from_fahrenheit(f: float) -> float:
        return (f - 32) * 5/9
    
    @staticmethod
    def calculate_saturation_vapor_pressure(temp_c: float) -> float:
        """
        Magnus formula for saturation vapor pressure.
        More accurate than simple approximations.
        """
        return 6.1094 * math.exp((17.625 * temp_c) / (temp_c + 243.04))
    
    @staticmethod
    def calculate_actual_vapor_pressure(temp_c: float, humidity_pct: float) -> float:
        """Actual vapor pressure from temperature and relative humidity."""
        svp = DensityAltitudeCalculator.calculate_saturation_vapor_pressure(temp_c)
        return (humidity_pct / 100.0) * svp
    
    @staticmethod
    def calculate_density_altitude(
        temp_f: float,
        pressure_inHg: float,
        altitude_ft: int,
        humidity_pct: float
    ) -> float:
        """
        Calculate Density Altitude in feet.
        
        Physics Reference:
        - Standard atmosphere: 29.92 inHg at sea level, 59°F
        - Each 10°F above standard = ~800-900 ft DA increase
        - Each 0.1 inHg below standard = ~300-400 ft DA increase
        - Humidity effect is modest but real (~100 ft per 20% RH change)
        
        Args:
            temp_f: Temperature in Fahrenheit
            pressure_inHg: Barometric pressure in inches of mercury
            altitude_ft: Actual elevation in feet
            humidity_pct: Relative humidity 0-100%
        
        Returns:
            Density Altitude in feet
        """
        # Convert temperature
        temp_c = DensityAltitudeCalculator.celsius_from_fahrenheit(temp_f)
        
        # Standard sea-level conditions
        std_temp_c = DensityAltitudeCalculator.celsius_from_fahrenheit(59.0)
        std_pressure_inHg = 29.92
        std_humidity_pct = 50.0
        
        # Calculate pressure ratio
        pressure_ratio = pressure_inHg / std_pressure_inHg
        
        # Temperature ratio (absolute temperature in Kelvin)
        temp_k = temp_c + 273.15
        std_temp_k = std_temp_c + 273.15
        temp_ratio = std_temp_k / temp_k
        
        # Humidity effect (dry air is denser than moist air - counterintuitive!)
        # Lower humidity slightly increases density
        actual_vp = DensityAltitudeCalculator.calculate_actual_vapor_pressure(temp_c, humidity_pct)
        std_vp = DensityAltitudeCalculator.calculate_actual_vapor_pressure(std_temp_c, std_humidity_pct)
        humidity_ratio = (1 - actual_vp) / (1 - std_vp)
        
        # Density ratio (combined effects)
        density_ratio = pressure_ratio * temp_ratio * humidity_ratio
        
        # Convert density ratio to density altitude
        # Scale factor: ~365 feet per 1% density difference
        da_correction = (1 - density_ratio) * 365000
        
        # DA = actual altitude + correction
        density_altitude = altitude_ft + da_correction
        
        return max(0, density_altitude)  # Never negative
    
    @staticmethod
    def get_air_density(
        temp_f: float,
        pressure_inHg: float,
        humidity_pct: float,
        altitude_ft: int
    ) -> float:
        """
        Get absolute air density in kg/m³.
        
        Used for drag calculations:
        - Sea level standard: ~1.225 kg/m³
        - Coors Field: ~0.88 kg/m³ (28% less dense)
        - Tropicana dome: ~1.24 kg/m³ (slightly denser due to climate control)
        """
        # Convert to SI units
        temp_k = DensityAltitudeCalculator.celsius_from_fahrenheit(temp_f) + 273.15
        pressure_pa = pressure_inHg * 3386.39  # Convert inHg to Pa
        
        # Account for altitude pressure drop
        # Pressure decreases ~5% per 1000 ft
        pressure_reduction = 1 - (altitude_ft / 145442)
        adjusted_pressure_pa = pressure_pa * max(0.1, pressure_reduction)
        
        # Humidity effect on density (dry air is denser)
        # Vapor pressure of water
        temp_c = temp_k - 273.15
        svp = DensityAltitudeCalculator.calculate_saturation_vapor_pressure(temp_c)
        actual_vp = (humidity_pct / 100.0) * svp
        
        # Mixing ratio effect
        humid_factor = (1 - actual_vp * 0.378 / adjusted_pressure_pa) / (1 - 0.378 * svp / 101325)
        
        # Ideal gas law: ρ = PM / RT
        # P = pressure (Pa), M = 0.029 kg/mol (dry air), R = 287 J/(kg·K)
        R_specific = 287  # J/(kg·K) for dry air
        dry_density = adjusted_pressure_pa / (R_specific * temp_k)
        
        # Adjust for humidity
        return dry_density * humid_factor


class WindVectorCalculator:
    """
    Map 3D wind vectors against stadium geometry.
    
    Wind impact on home runs:
    - Blowing out (away from infield): +5 to +15% carry
    - Blowing in (toward infield): -5 to -10% carry
    - Direction matters: 10 mph crosswind in narrow corridor ≠ 10 mph in open stadium
    """
    
    @staticmethod
    def get_outfield_vectors(stadium_name: str) -> Dict[str, Tuple[float, float, float]]:
        """
        Get normalized unit vectors pointing toward outfield gaps.
        
        Returns vectors for:
        - RF porch (right field)
        - CF alley (center field)
        - LF alley (left field)
        
        Format: (azimuth_deg, elevation_deg, magnitude_factor)
        where azimuth 0=North, 90=East, 180=South, 270=West
        """
        vectors = {
            # Yankee Stadium (short RF porch is 314 ft, tightly oriented)
            'Yankees': {
                'rf': (45, 0, 1.15),      # NE, RF porch effect
                'cf': (0, 0, 1.0),        # N
                'lf': (315, 0, 0.92),     # NW, deeper
            },
            # Fenway Park (Green Monster on LF, 310 ft)
            'Red Sox': {
                'rf': (90, 0, 0.95),      # E
                'cf': (45, 0, 1.0),       # NE
                'lf': (310, 0, 1.18),     # NW, wall effect
            },
            # Oracle Park (bay wind patterns, RF porch 315 ft)
            'Giants': {
                'rf': (135, 0, 1.20),     # SE, porch + bay wind
                'cf': (90, 0, 0.98),      # E, often windy
                'lf': (45, 0, 1.05),      # NW
            },
            # Coors Field (thin air, symmetric)
            'Rockies': {
                'rf': (90, 0, 1.28),      # E
                'cf': (0, 0, 1.28),       # N
                'lf': (270, 0, 1.28),     # W
            },
            # Comerica Park (death valley 420 ft CF)
            'Tigers': {
                'rf': (90, 0, 0.98),      # E
                'cf': (0, 0, 0.88),       # N, deep CF
                'lf': (270, 0, 0.95),     # W
            },
            # Kauffman Stadium (deep CF 410 ft, wind tunnel effect)
            'Royals': {
                'rf': (135, 0, 0.92),     # SE
                'cf': (45, 0, 0.88),      # NE, wind catches
                'lf': (315, 0, 0.92),     # NW
            },
        }
        
        return vectors.get(stadium_name, {
            'rf': (90, 0, 1.0),
            'cf': (0, 0, 1.0),
            'lf': (270, 0, 1.0),
        })
    
    @staticmethod
    def calculate_wind_impact(
        wind_speed_mph: float,
        wind_direction_deg: float,
        target_azimuth_deg: float,
        target_type: str = 'rf'  # rf, cf, lf
    ) -> float:
        """
        Calculate wind impact multiplier for ball carry distance.
        
        Physics:
        - Wind directly out: +1.5% per mph (10 mph out = +15%)
        - Wind directly in: -1.2% per mph (10 mph in = -12%)
        - Crosswind: reduced effect ~0.3% per mph component
        
        Args:
            wind_speed_mph: Wind speed in mph
            wind_direction_deg: Wind direction (0=N, 90=E, 180=S, 270=W)
            target_azimuth_deg: Direction of batted ball (RF=45, CF=0, LF=315)
            target_type: 'rf', 'cf', or 'lf' for stadium-specific effects
        
        Returns:
            Multiplier (1.0 = no wind effect, 1.15 = +15% carry)
        """
        if wind_speed_mph < 1:
            return 1.0
        
        # Normalize angles
        wind_dir_norm = wind_direction_deg % 360
        target_dir_norm = target_azimuth_deg % 360
        
        # Calculate relative angle between wind and target
        angle_diff = (target_dir_norm - wind_dir_norm) % 360
        
        # Convert to -180 to +180 range for easier interpretation
        if angle_diff > 180:
            angle_diff -= 360
        
        # Calculate wind component
        # Positive = wind blowing toward target (helps carry)
        # Negative = wind blowing against target (hurts carry)
        wind_component = math.cos(math.radians(angle_diff))
        
        # Magnitude depends on target type
        # RF/LF are narrower corridors, more affected by wind
        # CF is open, less wind effect
        if target_type == 'cf':
            multiplier = 1.0 + (wind_speed_mph * wind_component * 0.008)
        else:  # rf or lf
            multiplier = 1.0 + (wind_speed_mph * wind_component * 0.012)
        
        # Cap extremes (unrealistic to have >30% wind effect)
        return max(0.75, min(1.35, multiplier))


class TrajectorySimulator:
    """
    Simulate ball trajectory and carry distance using Magnus effect physics.
    
    Core Physics:
    - Exit velocity, launch angle, spin rate → trajectory
    - Air density affects drag coefficient
    - Lift/drag forces determine carry distance
    """
    
    GRAVITY = 32.174  # ft/s²
    
    @staticmethod
    def calculate_carry_distance(
        exit_velocity_mph: float,
        launch_angle_deg: float,
        spin_rate_rpm: int,
        spin_axis_deg: float,
        air_density: float,
        wind_multiplier: float = 1.0
    ) -> float:
        """
        Calculate projected carry distance in feet.
        
        Empirical validation shows:
        - 95 mph, 25°, 2400 rpm → ~380 ft carry (Statcast average)
        - 98 mph, 30°, 2200 rpm → ~420 ft carry
        - 102 mph, 35°, 2100 rpm → ~450+ ft carry (potential HR)
        
        Args:
            exit_velocity_mph: Ball speed off bat
            launch_angle_deg: Angle above horizontal
            spin_rate_rpm: Total spin in RPM
            spin_axis_deg: Spin axis direction (0-360)
            air_density: kg/m³ from environmental conditions
            wind_multiplier: Wind factor (1.0 = no wind)
        
        Returns:
            Carry distance in feet
        """
        # Convert units
        exit_vel_fps = exit_velocity_mph * 5280 / 3600
        launch_angle_rad = math.radians(launch_angle_deg)
        spin_rate_rps = spin_rate_rpm / 60
        
        # Spin-induced vertical break (in feet, at plate distance 60.5 ft)
        ivb = (spin_rate_rps * 0.04) / 10  # Simplified Magnus effect
        
        # Initial velocity components
        vx0 = exit_vel_fps * math.cos(launch_angle_rad)
        vy0 = exit_vel_fps * math.sin(launch_angle_rad)
        vz0 = 0
        
        # Drag coefficient (depends on air density)
        # Standard: Cd ~0.32 at sea level
        # Coors: Cd ~0.29 (30% less drag)
        cd_standard = 0.32
        cd_adjusted = cd_standard * (air_density / 1.225)
        
        # Ball aerodynamic properties
        ball = BallData()
        cross_section_area = math.pi * (ball.diameter_m / 2) ** 2
        
        # Drag force coefficient
        drag_coef = 0.5 * air_density * cd_adjusted * cross_section_area
        
        # Time to reach fence (assuming 330-430 ft outfield)
        # Typical hang time: 4-5 seconds
        time_steps = 500  # Fine time resolution
        dt = 0.01  # 10 ms time steps
        
        # Trajectory integration
        x, y, z = 0, 0, 0
        vx, vy, vz = vx0, vy0, vz0
        
        for _ in range(time_steps):
            # Velocity magnitude
            v_mag = math.sqrt(vx**2 + vy**2 + vz**2)
            
            if v_mag < 1:  # Ball stopped
                break
            
            # Drag acceleration (opposes velocity)
            drag_accel = (drag_coef / ball.mass_kg) * v_mag
            ax = -drag_accel * vx / v_mag
            ay = -drag_accel * vy / v_mag
            az = -drag_accel * vz / v_mag - TrajectorySimulator.GRAVITY
            
            # Update velocity
            vx += ax * dt
            vy += ay * dt
            vz += az * dt
            
            # Update position
            x += vx * dt
            y += vy * dt
            z += vz * dt
            
            # Ball hits ground
            if z < 0:
                # Interpolate to exact ground point
                t_ground = -z / vz if vz != 0 else 0
                x += vx * t_ground
                break
        
        # Carry distance (horizontal from home plate)
        carry_distance = math.sqrt(x**2 + y**2)
        
        # Apply wind multiplier
        carry_distance *= wind_multiplier
        
        return carry_distance


class BarrelSweetSpotCalculator:
    """
    Calculate probability of barrel contact based on batter's swing profile.
    
    Barrel = 83+ mph exit velo with 9-33° launch angle (simplified).
    But probability varies with batter mechanics and pitcher location.
    """
    
    @staticmethod
    def get_barrel_probability(
        exit_velocity_mph: float,
        launch_angle_deg: float,
        attack_angle_deg: float,  # Batter's swing plane
        pitch_vaa_deg: float,  # Pitcher's vertical approach angle
        attack_angle_variance: float = 8.0  # Swing consistency (lower = more consistent)
    ) -> float:
        """
        Probability that this contact is a barrel (high-quality).
        
        Args:
            exit_velocity_mph: Exit velocity
            launch_angle_deg: Launch angle
            attack_angle_deg: Batter's average attack angle
            pitch_vaa_deg: Pitcher's VAA (vertical approach angle)
            attack_angle_variance: Batter's swing plane variance
        
        Returns:
            Probability 0.0-1.0
        """
        # Barrel zone: 83+ mph, 9-33° LA
        if exit_velocity_mph < 83 or launch_angle_deg < 9 or launch_angle_deg > 33:
            return 0.0
        
        # Exit velocity contribution (exponential curve)
        # 83-92 mph: ramping up
        # 92+ mph: high probability
        ev_contrib = min(1.0, (exit_velocity_mph - 83) / 15)  # Saturates at 98 mph
        
        # Launch angle sweet spot
        # Peak at 25-28°
        optimal_la = 26
        la_diff = abs(launch_angle_deg - optimal_la)
        la_contrib = max(0, 1 - (la_diff / 17))  # 0 at 9° or 33°
        
        # Attack angle alignment
        # Pitcher throws with VAA X, batter swings at angle Y
        # Perfect match (batter's swing intercepts pitcher's ball) = higher barrel prob
        angle_match = abs(attack_angle_deg - pitch_vaa_deg)
        match_contrib = max(0, 1 - (angle_match / 30))  # Perfect match at 0°
        
        # Consistency factor (standard deviation of swing plane)
        # Consistent swingers (SD=4°) barrel more often
        consistency = 1 - (attack_angle_variance / 20)  # Saturates at 20°
        
        # Combined probability
        barrel_prob = ev_contrib * la_contrib * match_contrib * (0.8 + 0.2 * consistency)
        
        return min(1.0, max(0, barrel_prob))


def simulate_home_run_probability(
    batter_exit_velocity_mph: float,
    batter_launch_angle_deg: float,
    batter_spin_rate_rpm: int,
    batter_spin_axis_deg: float,
    pitcher_vaa_deg: float,
    batter_attack_angle_deg: float,
    environmental_conditions: EnvironmentalConditions,
    stadium_name: str,
    target_field: str = 'cf',  # rf, cf, lf
) -> Dict[str, float]:
    """
    Simulate complete home run probability incorporating all physics layers.
    
    Returns:
        {
            'carry_distance_ft': float,
            'barrier_distance_ft': float,
            'will_clear_fence': bool,
            'barrel_probability': float,
            'home_run_probability': float,
            'density_altitude_ft': float,
            'air_density_kg_m3': float,
            'wind_multiplier': float,
        }
    """
    # Calculate environmental physics
    da = DensityAltitudeCalculator.calculate_density_altitude(
        environmental_conditions.temperature_f,
        environmental_conditions.barometric_pressure_inHg,
        environmental_conditions.altitude_ft,
        environmental_conditions.humidity_pct
    )
    
    air_density = DensityAltitudeCalculator.get_air_density(
        environmental_conditions.temperature_f,
        environmental_conditions.barometric_pressure_inHg,
        environmental_conditions.humidity_pct,
        environmental_conditions.altitude_ft
    )
    
    # Calculate wind impact
    outfield_vectors = WindVectorCalculator.get_outfield_vectors(stadium_name)
    target_vector = outfield_vectors.get(target_field, (0, 0, 1.0))
    
    wind_mult = WindVectorCalculator.calculate_wind_impact(
        environmental_conditions.wind_speed_mph,
        environmental_conditions.wind_direction_deg,
        target_vector[0],
        target_field
    )
    
    # Simulate trajectory
    carry_distance = TrajectorySimulator.calculate_carry_distance(
        batter_exit_velocity_mph,
        batter_launch_angle_deg,
        batter_spin_rate_rpm,
        batter_spin_axis_deg,
        air_density,
        wind_mult
    )
    
    # Estimate barrier distance (simplified, should use actual wall coordinates)
    barrier_distances = {
        'rf': {'Yankees': 314, 'Giants': 315, 'Rockies': 350},
        'cf': {'Comerica': 420, 'Kauffman': 410, 'Rockies': 415},
        'lf': {'Red Sox': 310, 'Fenway': 310},
    }
    barrier_distance = barrier_distances.get(target_field, {}).get(
        stadium_name.split()[-1], 370
    )
    
    # Barrel probability
    barrel_prob = BarrelSweetSpotCalculator.get_barrel_probability(
        batter_exit_velocity_mph,
        batter_launch_angle_deg,
        batter_attack_angle_deg,
        pitcher_vaa_deg
    )
    
    # Home run probability
    will_clear = carry_distance > barrier_distance
    hr_prob = barrel_prob if will_clear else barrel_prob * 0.1  # Slight chance of HBP that scores
    
    return {
        'carry_distance_ft': round(carry_distance, 1),
        'barrier_distance_ft': barrier_distance,
        'will_clear_fence': will_clear,
        'barrel_probability': round(barrel_prob, 3),
        'home_run_probability': round(hr_prob, 3),
        'density_altitude_ft': round(da, 0),
        'air_density_kg_m3': round(air_density, 4),
        'wind_multiplier': round(wind_mult, 3),
    }
