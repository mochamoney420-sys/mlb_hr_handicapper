import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, '.')

from src import free_odds_sources as fos


class FreeOddsSourcesTests(unittest.TestCase):
    def test_load_free_odds_sources_reads_default_data_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                data_dir = Path('data')
                data_dir.mkdir(exist_ok=True)
                odds_path = data_dir / 'hr_prop_odds.json'
                odds_path.write_text(
                    json.dumps({'Hunter Goodman': {'draftkings': -120}}),
                    encoding='utf-8',
                )

                with patch.dict(os.environ, {
                    'FREE_ODDS_JSON_PATH': '',
                    'FREE_ODDS_CSV_PATH': '',
                    'FREE_ODDS_PUBLIC_URLS': '',
                }, clear=False):
                    loaded = fos.load_free_odds_sources()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(loaded, {'Hunter Goodman': {'draftkings': -120}})


if __name__ == '__main__':
    unittest.main()
