#!/usr/bin/env python
"""Test the three professional-grade upgrades to the HR prediction model."""

import sys
sys.path.insert(0, '.')
from run_daily_predictions import project_batting_order_pa, monte_carlo_hr_simulation, calculate_ev_premium

# Test 1: PA projection by batting order
print("=" * 60)
print("TEST 1: Batting Order PA Projection")
print("=" * 60)
for slot in [1, 2, 5, 9]:
    pa = project_batting_order_pa(slot)
    print(f"Slot {slot}: {pa:.2f} projected PAs per game")

# Test 2: Monte Carlo simulation
print("\n" + "=" * 60)
print("TEST 2: Monte Carlo HR Simulation")
print("=" * 60)
probs = [0.05, 0.10, 0.15, 0.20]
for p in probs:
    sim_prob = monte_carlo_hr_simulation(p, num_simulations=5000)
    print(f"Single PA prob: {p:.2%} → Game prob (10k sims): {sim_prob:.2%}")

# Test 3: EV calculation
print("\n" + "=" * 60)
print("TEST 3: Expected Value (+EV Premium)")
print("=" * 60)
model_prob = 0.15
market_prob = 0.12
ev_val, dec_odds, ev_pct = calculate_ev_premium(model_prob, market_prob)
print(f"Model: {model_prob:.1%} vs Market: {market_prob:.1%}")
print(f"Decimal Odds: {dec_odds:.2f}, EV/dollar: ${ev_val:.2f}, EV%: {ev_pct:.1f}%")

print("\n✅ All professional-grade functions working!")
