from pathlib import Path
import run_daily_predictions as rdp
src = Path(rdp.__file__).read_text(encoding='utf-8').splitlines()
for i, l in enumerate(src, 1):
    if 'physics_delta_abs' in l or "if 'physics_delta' not in radar.columns" in l or "radar['physics_delta']" in l:
        print(i, l)
