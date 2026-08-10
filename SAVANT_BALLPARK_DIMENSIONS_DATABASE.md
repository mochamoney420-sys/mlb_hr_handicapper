# Savant Ballpark Dimensions Database

This project now supports loading a Baseball Savant stadium-dimensions export and using it to override static park geometry and park factors.

## What It Does

When enabled, the loader in [src/ballpark_dimensions.py](src/ballpark_dimensions.py) will:

- Read `data/savant_ballpark_dimensions.csv` (or a custom path).
- Match rows to MLB parks by team abbreviation or stadium name.
- Override per-park geometry (RF/LF/CF distances and wall heights).
- Derive handedness-specific HR park factors from geometry and elevation.
- Tag park records with `savant_3d` in characteristics.

## File Location

Default CSV path:

- `data/savant_ballpark_dimensions.csv`

Optional override via env var:

- `SAVANT_BALLPARK_DIMENSIONS_CSV`

Enable/disable loader:

- `SAVANT_BALLPARK_DIMENSIONS_ENABLED=true|false` (default: `true`)

## Required CSV Columns

Use any of these accepted column names per field.

- Team: `team_abbreviation` or `team_abbr` or `team` or `home_team`
- Stadium Name: `stadium_name` or `venue_name` or `park_name` or `stadium`
- RF Distance: `rf_distance` or `right_field_distance` or `rf`
- LF Distance: `lf_distance` or `left_field_distance` or `lf`
- CF Distance: `cf_distance` or `center_field_distance` or `cf`

## Optional CSV Columns

- RF Wall Height: `rf_wall_height` or `right_field_wall_height`
- LF Wall Height: `lf_wall_height` or `left_field_wall_height`
- CF Wall Height: `cf_wall_height` or `center_field_wall_height`
- Elevation Feet: `elevation_ft` or `elevation` or `altitude_ft`

## Minimal Example

```csv
team_abbreviation,stadium_name,rf_distance,lf_distance,cf_distance,rf_wall_height,lf_wall_height,cf_wall_height,elevation_ft
NYY,Yankee Stadium,314,318,408,8,8,8,55
BOS,Fenway Park,302,310,390,3,37,17,20
COL,Coors Field,350,347,415,8,8,8,5200
```

## Runtime Behavior

- If the CSV is missing, the model uses built-in park data.
- If the CSV is present and valid, overrides are applied at import time.
- A startup log line confirms how many parks were updated.

## Notes

- Handedness mapping in factor derivation uses pull side:
  - RHH pull side -> LF dimensions
  - LHH pull side -> RF dimensions
- Derived factors are bounded to stable ranges for production safety.
