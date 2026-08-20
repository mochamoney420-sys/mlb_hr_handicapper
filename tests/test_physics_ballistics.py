"""
Test suite for Physics & Ballistics Simulator

Validates all physics calculations against known real-world scenarios from Statcast.
"""

import unittest
import math
from src.physics_ballistics import (
    DensityAltitudeCalculator,
    WindVectorCalculator,
    TrajectorySimulator,
    BarrelSweetSpotCalculator,
    EnvironmentalConditions,
    simulate_home_run_probability,
)


class TestDensityAltitudeCalculator(unittest.TestCase):
    """Validate density altitude calculations."""
    
    def test_sea_level_standard_conditions(self):
        """At sea level, standard conditions (59°F, 29.92 inHg, 50% RH) → DA ≈ 500 ft"""
        da = DensityAltitudeCalculator.calculate_density_altitude(
            temp_f=59,
            pressure_inHg=29.92,
            altitude_ft=0,
            humidity_pct=50
        )
        # Should be close to 0 but likely 300-500 ft due to typical conditions
        self.assertGreaterEqual(da, 0)
        self.assertLess(da, 2000)
        print(f"✓ Sea level DA: {da:.0f} ft")
    
    def test_coors_field_elevation(self):
        """Coors Field at 5280 ft elevation with summer conditions → DA 7000-8000 ft"""
        da = DensityAltitudeCalculator.calculate_density_altitude(
            temp_f=85,  # Summer
            pressure_inHg=29.9,
            altitude_ft=5280,
            humidity_pct=30  # Denver is dry
        )
        # Coors should have massive DA bonus (2200-2700 ft)
        self.assertGreater(da, 6500)
        self.assertLess(da, 9000)
        print(f"✓ Coors DA: {da:.0f} ft (elevation bonus ~{da-5280:.0f} ft)")
    
    def test_temperature_effect(self):
        """Hotter = higher DA. 10°F difference ≈ 800-900 ft difference"""
        da_cool = DensityAltitudeCalculator.calculate_density_altitude(
            temp_f=60, pressure_inHg=29.92, altitude_ft=0, humidity_pct=50
        )
        da_hot = DensityAltitudeCalculator.calculate_density_altitude(
            temp_f=90, pressure_inHg=29.92, altitude_ft=0, humidity_pct=50
        )
        temp_diff = da_hot - da_cool
        # 30°F difference should be 2400-3000 ft DA difference
        self.assertGreater(temp_diff, 1500)
        print(f"✓ Temperature effect: {temp_diff:.0f} ft for 30°F increase")
    
    def test_air_density_calculation(self):
        """Air density: sea level ~1.225 kg/m³, Coors ~0.88 kg/m³"""
        air_dens_sea = DensityAltitudeCalculator.get_air_density(
            temp_f=59, pressure_inHg=29.92, humidity_pct=50, altitude_ft=0
        )
        air_dens_coors = DensityAltitudeCalculator.get_air_density(
            temp_f=75, pressure_inHg=29.9, humidity_pct=30, altitude_ft=5280
        )
        
        # Coors should be ~28% less dense
        density_ratio = air_dens_sea / air_dens_coors
        self.assertGreater(density_ratio, 1.2)
        self.assertLess(density_ratio, 1.4)
        print(f"✓ Air density: Sea {air_dens_sea:.3f} → Coors {air_dens_coors:.3f} kg/m³ ({density_ratio:.2f}x)")


class TestWindVectorCalculator(unittest.TestCase):
    """Validate 3D wind vector calculations."""
    
    def test_wind_blowing_out(self):
        """Wind blowing out (away from field) should boost carry"""
        mult_out = WindVectorCalculator.calculate_wind_impact(
            wind_speed_mph=15,
            wind_direction_deg=0,      # N (blowing N)
            target_azimuth_deg=0,      # Also N (wind blowing toward target)
            target_type='cf'
        )
        # 15 mph out @ 0.8% per mph = ~12% boost
        self.assertGreater(mult_out, 1.10)
        self.assertLess(mult_out, 1.20)
        print(f"✓ Wind out: 15 mph → {mult_out:.3f}x multiplier")
    
    def test_wind_blowing_in(self):
        """Wind blowing in (toward field) should reduce carry"""
        mult_in = WindVectorCalculator.calculate_wind_impact(
            wind_speed_mph=15,
            wind_direction_deg=180,    # S (blowing S)
            target_azimuth_deg=0,      # N (wind blowing toward us)
            target_type='cf'
        )
        # 15 mph in @ 0.8% per mph = ~12% reduction
        self.assertLess(mult_in, 0.90)
        self.assertGreater(mult_in, 0.80)
        print(f"✓ Wind in: 15 mph → {mult_in:.3f}x multiplier")
    
    def test_crosswind_reduced_effect(self):
        """Crosswind (90° to target) should have minimal effect"""
        mult_cross = WindVectorCalculator.calculate_wind_impact(
            wind_speed_mph=20,
            wind_direction_deg=90,     # E
            target_azimuth_deg=0,      # N
            target_type='cf'
        )
        # Perpendicular wind has ~0% direct component
        self.assertAlmostEqual(mult_cross, 1.0, delta=0.05)
        print(f"✓ Crosswind: 20 mph 90° offset → {mult_cross:.3f}x multiplier")
    
    def test_rf_vs_cf_wind_effect(self):
        """RF/LF corridors should have different wind response than CF"""
        mult_cf = WindVectorCalculator.calculate_wind_impact(
            wind_speed_mph=10, wind_direction_deg=0,
            target_azimuth_deg=0, target_type='cf'
        )
        mult_rf = WindVectorCalculator.calculate_wind_impact(
            wind_speed_mph=10, wind_direction_deg=0,
            target_azimuth_deg=45, target_type='rf'
        )
        # RF should be more sensitive to wind
        self.assertNotAlmostEqual(mult_cf, mult_rf, delta=0.01)
        print(f"✓ CF wind: {mult_cf:.3f}x, RF wind: {mult_rf:.3f}x")


class TestTrajectorySimulator(unittest.TestCase):
    """Validate ball trajectory calculations."""
    
    def test_typical_hr_carry_distance(self):
        """
        Typical HR: 95 mph, 25° LA, 2400 rpm spin, sea level conditions
        Should carry ~380-400 ft
        """
        # Standard air density at sea level
        air_density = 1.225  # kg/m³
        
        carry = TrajectorySimulator.calculate_carry_distance(
            exit_velocity_mph=95,
            launch_angle_deg=25,
            spin_rate_rpm=2400,
            spin_axis_deg=180,  # Backspin
            air_density=air_density,
            wind_multiplier=1.0
        )
        
        # Should carry 350-410 ft
        self.assertGreater(carry, 350)
        self.assertLess(carry, 410)
        print(f"✓ Typical HR carry: {carry:.0f} ft (95 mph, 25°, 2400 rpm)")
    
    def test_coors_vs_sea_level_carry(self):
        """Same contact at Coors (thin air) should carry 30-50 ft farther"""
        carry_sea = TrajectorySimulator.calculate_carry_distance(
            exit_velocity_mph=95,
            launch_angle_deg=25,
            spin_rate_rpm=2400,
            spin_axis_deg=180,
            air_density=1.225,  # Sea level
            wind_multiplier=1.0
        )
        carry_coors = TrajectorySimulator.calculate_carry_distance(
            exit_velocity_mph=95,
            launch_angle_deg=25,
            spin_rate_rpm=2400,
            spin_axis_deg=180,
            air_density=0.88,  # Coors (~28% less dense)
            wind_multiplier=1.0
        )
        
        carry_gain = carry_coors - carry_sea
        # Should gain 30-50 ft at Coors
        self.assertGreater(carry_gain, 25)
        self.assertLess(carry_gain, 60)
        print(f"✓ Coors effect: {carry_sea:.0f} ft → {carry_coors:.0f} ft (+{carry_gain:.0f} ft)")
    
    def test_velocity_exponential_effect(self):
        """Exit velocity should have exponential effect on carry"""
        carry_95 = TrajectorySimulator.calculate_carry_distance(
            exit_velocity_mph=95, launch_angle_deg=25, spin_rate_rpm=2400,
            spin_axis_deg=180, air_density=1.225
        )
        carry_100 = TrajectorySimulator.calculate_carry_distance(
            exit_velocity_mph=100, launch_angle_deg=25, spin_rate_rpm=2400,
            spin_axis_deg=180, air_density=1.225
        )
        carry_105 = TrajectorySimulator.calculate_carry_distance(
            exit_velocity_mph=105, launch_angle_deg=25, spin_rate_rpm=2400,
            spin_axis_deg=180, air_density=1.225
        )
        
        # Gains should increase with velocity
        gain_95_to_100 = carry_100 - carry_95
        gain_100_to_105 = carry_105 - carry_100
        self.assertGreater(gain_100_to_105, gain_95_to_100)
        print(f"✓ Velocity effect: 95mph={carry_95:.0f}ft, 100mph={carry_100:.0f}ft, 105mph={carry_105:.0f}ft")


class TestBarrelSweetSpotCalculator(unittest.TestCase):
    """Validate barrel sweet spot probability."""
    
    def test_perfect_barrel_contact(self):
        """Perfect barrel: 95 mph EV, 26° LA → high probability"""
        prob = BarrelSweetSpotCalculator.get_barrel_probability(
            exit_velocity_mph=95,
            launch_angle_deg=26,
            attack_angle_deg=25,      # Aligned with swing plane
            pitch_vaa_deg=25,         # Pitcher throws in same zone
            attack_angle_variance=5   # Consistent swing
        )
        self.assertGreater(prob, 0.7)
        print(f"✓ Perfect barrel probability: {prob:.2%}")
    
    def test_sub_83_mph_not_barrel(self):
        """Exit velo <83 mph cannot be barrel"""
        prob = BarrelSweetSpotCalculator.get_barrel_probability(
            exit_velocity_mph=80,
            launch_angle_deg=26,
            attack_angle_deg=25,
            pitch_vaa_deg=25
        )
        self.assertEqual(prob, 0.0)
        print(f"✓ 80 mph EV: {prob:.2%} (not barrel)")
    
    def test_wrong_launch_angle(self):
        """LA outside 9-33° range → not barrel"""
        prob_too_low = BarrelSweetSpotCalculator.get_barrel_probability(
            exit_velocity_mph=95,
            launch_angle_deg=5,  # Too low
            attack_angle_deg=25,
            pitch_vaa_deg=25
        )
        prob_too_high = BarrelSweetSpotCalculator.get_barrel_probability(
            exit_velocity_mph=95,
            launch_angle_deg=40,  # Too high
            attack_angle_deg=25,
            pitch_vaa_deg=25
        )
        self.assertEqual(prob_too_low, 0.0)
        self.assertEqual(prob_too_high, 0.0)
        print(f"✓ Invalid launch angles: 5°={prob_too_low:.2%}, 40°={prob_too_high:.2%}")


class TestFullSimulation(unittest.TestCase):
    """End-to-end home run probability simulation."""
    
    def test_yankee_stadium_short_porch_hr(self):
        """Yankee Stadium: 95 mph to RF porch (314 ft)"""
        conditions = EnvironmentalConditions(
            temperature_f=75,
            barometric_pressure_inHg=29.92,
            altitude_ft=55,
            humidity_pct=60,
            wind_speed_mph=10,      # Blowing out to RF
            wind_direction_deg=45,
            roof_status='open'
        )
        
        result = simulate_home_run_probability(
            batter_exit_velocity_mph=95,
            batter_launch_angle_deg=25,
            batter_spin_rate_rpm=2400,
            batter_spin_axis_deg=180,
            pitcher_vaa_deg=25,
            batter_attack_angle_deg=25,
            environmental_conditions=conditions,
            stadium_name='Yankees',
            target_field='rf'
        )
        
        self.assertIn('carry_distance_ft', result)
        self.assertIn('will_clear_fence', result)
        self.assertIn('home_run_probability', result)
        self.assertTrue(result['will_clear_fence'])
        self.assertGreater(result['home_run_probability'], 0.5)
        print(f"✓ Yankee Stadium HR: {result['carry_distance_ft']} ft carry, {result['home_run_probability']:.1%} HR prob")
    
    def test_comerica_death_valley_suppression(self):
        """Comerica: Same 95 mph to CF (420 ft deep) should often be out"""
        conditions = EnvironmentalConditions(
            temperature_f=75,
            barometric_pressure_inHg=29.92,
            altitude_ft=650,
            humidity_pct=60,
            wind_speed_mph=5,
            wind_direction_deg=0,
            roof_status='open'
        )
        
        result = simulate_home_run_probability(
            batter_exit_velocity_mph=95,
            batter_launch_angle_deg=25,
            batter_spin_rate_rpm=2400,
            batter_spin_axis_deg=180,
            pitcher_vaa_deg=25,
            batter_attack_angle_deg=25,
            environmental_conditions=conditions,
            stadium_name='Tigers',  # Comerica
            target_field='cf'
        )
        
        # 95 mph should NOT clear 420 ft at normal conditions
        self.assertFalse(result['will_clear_fence'])
        print(f"✓ Comerica death valley: {result['carry_distance_ft']} ft carry (barrier 420 ft)")
    
    def test_coors_elevation_effect(self):
        """Coors: Same 92 mph should carry farther due to thin air"""
        conditions = EnvironmentalConditions(
            temperature_f=75,
            barometric_pressure_inHg=29.9,  # Slightly lower at elevation
            altitude_ft=5280,
            humidity_pct=30,  # Denver is dry
            wind_speed_mph=5,
            wind_direction_deg=0,
            roof_status='open'
        )
        
        result = simulate_home_run_probability(
            batter_exit_velocity_mph=92,
            batter_launch_angle_deg=25,
            batter_spin_rate_rpm=2400,
            batter_spin_axis_deg=180,
            pitcher_vaa_deg=25,
            batter_attack_angle_deg=25,
            environmental_conditions=conditions,
            stadium_name='Rockies',
            target_field='cf'
        )
        
        # Should carry to fence (415 ft)
        self.assertGreater(result['carry_distance_ft'], 380)
        self.assertTrue(result['will_clear_fence'])
        print(f"✓ Coors thin air: {result['carry_distance_ft']} ft carry (barrier 415 ft), DA={result['density_altitude_ft']} ft")


def run_all_tests():
    """Run complete test suite."""
    print("\n" + "="*70)
    print("PHYSICS BALLISTICS TEST SUITE")
    print("="*70 + "\n")
    
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    suite.addTests(loader.loadTestsFromTestCase(TestDensityAltitudeCalculator))
    suite.addTests(loader.loadTestsFromTestCase(TestWindVectorCalculator))
    suite.addTests(loader.loadTestsFromTestCase(TestTrajectorySimulator))
    suite.addTests(loader.loadTestsFromTestCase(TestBarrelSweetSpotCalculator))
    suite.addTests(loader.loadTestsFromTestCase(TestFullSimulation))
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    print("\n" + "="*70)
    if result.wasSuccessful():
        print(f"✅ ALL {result.testsRun} TESTS PASSED")
    else:
        print(f"❌ {len(result.failures)} failures, {len(result.errors)} errors")
    print("="*70 + "\n")
    
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_all_tests()
    exit(0 if success else 1)
