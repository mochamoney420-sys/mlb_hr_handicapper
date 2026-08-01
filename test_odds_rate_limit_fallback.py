import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, '.')

import run_daily_predictions as rdp


class OddsFallbackTests(unittest.TestCase):
    def test_fetch_hr_prop_odds_prefers_free_sources(self):
        with patch.dict(os.environ, {'ODDS_API_KEY': 'dummy-key', 'ODDS_USE_FREE_FALLBACK': 'true', 'ODDS_PREFER_FREE_SOURCES': 'true'}, clear=False):
            with patch.object(rdp, '_odds_invalid_key_cooldown_active', return_value=False), \
                 patch.object(rdp, '_rate_limit_cooldown_active', return_value=False), \
                 patch.object(rdp, 'load_free_odds_sources', return_value={'Player A': {'draftkings': -110}}), \
                 patch.object(rdp, 'build_devigged_probs_from_books', return_value={'Player A': 0.5238}), \
                 patch.object(rdp, '_fetch_hr_props_raw_from_sportsgameodds', return_value={'Player B': {'draftkings': -120}}):
                probs = rdp.fetch_hr_prop_odds()

        self.assertEqual(probs, {'Player A': 0.5238})

    def test_fetch_hr_prop_odds_uses_cached_payload_when_rate_limited(self):
        with patch.dict(os.environ, {'ODDS_API_KEY': 'dummy-key', 'ODDS_USE_FREE_FALLBACK': 'true', 'ODDS_PREFER_FREE_SOURCES': 'true'}, clear=False):
            with patch.object(rdp, '_odds_invalid_key_cooldown_active', return_value=False), \
                 patch.object(rdp, '_rate_limit_cooldown_active', return_value=True), \
                 patch.object(rdp, 'load_free_odds_sources', return_value={}), \
                 patch.object(rdp, 'build_devigged_probs_from_books', return_value={}), \
                 patch.object(rdp, '_load_cached_hr_prop_odds_payload', return_value=({'Player A': {'draftkings': -110}}, 42)):
                probs = rdp.fetch_hr_prop_odds()

        self.assertEqual(probs, {'Player A': 1.0})


if __name__ == '__main__':
    unittest.main()
