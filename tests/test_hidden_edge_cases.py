import pathlib

import numpy as np
import pandas as pd

from src.ballpark_dimensions import get_ballpark_factor
from src.professional_bettors import identify_platoon_mismatches


def test_ballpark_aliases_accept_team_abbreviations():
    assert get_ballpark_factor('BOS', 'R')['park_factor'] > 0
    assert get_ballpark_factor('NYY', 'R')['park_factor'] > 0
    assert get_ballpark_factor('Red Sox', 'R')['park_factor'] == get_ballpark_factor('BOS', 'R')['park_factor']
    assert get_ballpark_factor('Yankees', 'R')['park_factor'] == get_ballpark_factor('NYY', 'R')['park_factor']


def test_platoon_multiplier_is_finite_and_non_nan():
    df = pd.DataFrame([
        {'pitcher': 42, 'stand': 'R', 'type': 'pitch', 'events': 'home_run', 'bb_type': 'fly_ball'}
        for _ in range(25)
    ])
    mult = identify_platoon_mismatches(99, 42, 'R', df)
    assert np.isfinite(mult)
    assert mult >= 1.0


def test_pytest_targets_the_isolated_tests_folder():
    contents = pathlib.Path('pytest.ini').read_text()
    assert 'testpaths = tests' in contents
