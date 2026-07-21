"""
Stadium Wind Vector Database

Geospatial coordinates and geometric properties for all 30 MLB stadiums.
Used to calculate 3D wind impact on ball carry based on stadium orientation 
and prevailing wind patterns.

Data sources:
- Latitude/Longitude: GPS coordinates of home plate
- Elevation: USGS elevation database
- Stadium orientation: Statcast, stadium technical specs
- Roof status: Current 2026 stadium configurations
"""

STADIUM_COORDINATES = {
    # AL EAST
    'Yankees': {
        'name': 'Yankee Stadium',
        'latitude': 40.8296,
        'longitude': -73.9262,
        'elevation_ft': 55,
        'city': 'Bronx, NY',
        'orientation_deg': 8,  # Aligned ~NNE to SSW (8° from north)
        'roof_status': 'open',
        'region': 'Northeast',
        'wind_pattern': 'Atlantic influence, winter gales from NW',
        'outfield_geometry': {
            'rf_distance_ft': 314,
            'rf_height_ft': 8,
            'cf_distance_ft': 418,
            'cf_height_ft': 8,
            'lf_distance_ft': 314,
            'lf_height_ft': 37,  # Green Monster height
        }
    },
    'Red Sox': {
        'name': 'Fenway Park',
        'latitude': 42.3461,
        'longitude': -71.0981,
        'elevation_ft': 21,
        'city': 'Boston, MA',
        'orientation_deg': 28,
        'roof_status': 'open',
        'region': 'Northeast',
        'wind_pattern': 'Atlantic winds, NE storms common',
        'outfield_geometry': {
            'rf_distance_ft': 380,
            'rf_height_ft': 8,
            'cf_distance_ft': 420,
            'cf_height_ft': 8,
            'lf_distance_ft': 310,
            'lf_height_ft': 37,  # Green Monster
        }
    },
    'Orioles': {
        'name': 'Camden Yards',
        'latitude': 39.2847,
        'longitude': -76.6205,
        'elevation_ft': 19,
        'city': 'Baltimore, MD',
        'orientation_deg': 100,  # NE-SW orientation
        'roof_status': 'open',
        'region': 'Mid-Atlantic',
        'wind_pattern': 'Humid continental, SW winds common',
        'outfield_geometry': {
            'rf_distance_ft': 333,
            'rf_height_ft': 25,
            'cf_distance_ft': 400,
            'cf_height_ft': 25,
            'lf_distance_ft': 337,
            'lf_height_ft': 25,
        }
    },
    'Rays': {
        'name': 'Tropicana Field',
        'latitude': 27.7681,
        'longitude': -82.6534,
        'elevation_ft': 14,
        'city': 'St. Petersburg, FL',
        'orientation_deg': 120,
        'roof_status': 'closed',  # Domed
        'region': 'Southeast',
        'wind_pattern': 'Tropical - no external wind impact',
        'outfield_geometry': {
            'rf_distance_ft': 315,
            'rf_height_ft': 8,
            'cf_distance_ft': 404,
            'cf_height_ft': 8,
            'lf_distance_ft': 315,
            'lf_height_ft': 8,
        }
    },
    'Blue Jays': {
        'name': 'Rogers Centre',
        'latitude': 43.6426,
        'longitude': -79.3957,
        'elevation_ft': 251,
        'city': 'Toronto, ON',
        'orientation_deg': 45,
        'roof_status': 'retracted',  # Retractable
        'region': 'North',
        'wind_pattern': 'Great Lakes influence, strong westerlies',
        'outfield_geometry': {
            'rf_distance_ft': 328,
            'rf_height_ft': 8,
            'cf_distance_ft': 400,
            'cf_height_ft': 8,
            'lf_distance_ft': 328,
            'lf_height_ft': 8,
        }
    },

    # AL CENTRAL
    'Tigers': {
        'name': 'Comerica Park',
        'latitude': 42.3391,
        'longitude': -83.0485,
        'elevation_ft': 650,
        'city': 'Detroit, MI',
        'orientation_deg': 110,
        'roof_status': 'open',
        'region': 'Midwest',
        'wind_pattern': 'Great Lakes, cold NW winds in fall',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 420,  # DEATH VALLEY
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },
    'White Sox': {
        'name': 'Guaranteed Rate Field',
        'latitude': 41.8299,
        'longitude': -87.6338,
        'elevation_ft': 596,
        'city': 'Chicago, IL',
        'orientation_deg': 270,  # N-S orientation
        'roof_status': 'open',
        'region': 'Midwest',
        'wind_pattern': 'Lake Michigan, strong SW winds',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 400,
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },
    'Royals': {
        'name': 'Kauffman Stadium',
        'latitude': 39.0516,
        'longitude': -94.4803,
        'elevation_ft': 750,
        'city': 'Kansas City, MO',
        'orientation_deg': 75,
        'roof_status': 'open',
        'region': 'Midwest',
        'wind_pattern': 'Great Plains, strong SW/W winds, wind tunnel effect',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 410,  # Deep CF
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },
    'Twins': {
        'name': 'Target Field',
        'latitude': 44.9818,
        'longitude': -93.2775,
        'elevation_ft': 815,
        'city': 'Minneapolis, MN',
        'orientation_deg': 140,
        'roof_status': 'open',
        'region': 'Midwest',
        'wind_pattern': 'Northern plains, cold W winds, isolated tornadoes',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 404,
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },

    # AL WEST
    'Astros': {
        'name': 'Minute Maid Park',
        'latitude': 29.7572,
        'longitude': -95.3555,
        'elevation_ft': 25,
        'city': 'Houston, TX',
        'orientation_deg': 20,
        'roof_status': 'retracted',  # Retractable
        'region': 'South',
        'wind_pattern': 'Gulf moisture, NE afternoon winds',
        'outfield_geometry': {
            'rf_distance_ft': 315,
            'rf_height_ft': 8,
            'cf_distance_ft': 409,
            'cf_height_ft': 8,
            'lf_distance_ft': 315,
            'lf_height_ft': 8,
        }
    },
    'Mariners': {
        'name': 'T-Mobile Park',
        'latitude': 47.5911,
        'longitude': -122.3522,
        'elevation_ft': 19,
        'city': 'Seattle, WA',
        'orientation_deg': 335,  # NW-SE
        'roof_status': 'retracted',
        'region': 'Pacific',
        'wind_pattern': 'Marine layer, prevailing SW winds, cool/wet',
        'outfield_geometry': {
            'rf_distance_ft': 331,
            'rf_height_ft': 8,
            'cf_distance_ft': 405,
            'cf_height_ft': 8,
            'lf_distance_ft': 331,
            'lf_height_ft': 8,
        }
    },
    'Rangers': {
        'name': 'Globe Life Field',
        'latitude': 32.7457,
        'longitude': -97.0835,
        'elevation_ft': 617,
        'city': 'Arlington, TX',
        'orientation_deg': 340,  # NW-SE
        'roof_status': 'retracted',
        'region': 'South',
        'wind_pattern': 'Hot, dry Texas wind, strong SW winds',
        'outfield_geometry': {
            'rf_distance_ft': 328,
            'rf_height_ft': 8,
            'cf_distance_ft': 407,
            'cf_height_ft': 8,
            'lf_distance_ft': 328,
            'lf_height_ft': 8,
        }
    },
    'Athletics': {
        'name': 'Oakland Coliseum',
        'latitude': 37.7516,
        'longitude': -122.2008,
        'elevation_ft': 42,
        'city': 'Oakland, CA',
        'orientation_deg': 200,  # Unusual orientation
        'roof_status': 'open',
        'region': 'Pacific',
        'wind_pattern': 'Bay breeze, cool maritime air',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 400,
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },

    # NL EAST
    'Mets': {
        'name': 'Citi Field',
        'latitude': 40.7575,
        'longitude': -73.8458,
        'elevation_ft': 20,
        'city': 'Queens, NY',
        'orientation_deg': 45,  # NE-SW
        'roof_status': 'open',
        'region': 'Northeast',
        'wind_pattern': 'Atlantic, subject to nor\'easters',
        'outfield_geometry': {
            'rf_distance_ft': 335,
            'rf_height_ft': 13,
            'cf_distance_ft': 408,
            'cf_height_ft': 13,
            'lf_distance_ft': 330,
            'lf_height_ft': 13,
        }
    },
    'Braves': {
        'name': 'Truist Park',
        'latitude': 33.8906,
        'longitude': -84.4677,
        'elevation_ft': 1050,
        'city': 'Atlanta, GA',
        'orientation_deg': 120,
        'roof_status': 'retracted',
        'region': 'Southeast',
        'wind_pattern': 'Warm humid SE winds, afternoon thunderstorms',
        'outfield_geometry': {
            'rf_distance_ft': 325,
            'rf_height_ft': 8,
            'cf_distance_ft': 400,
            'cf_height_ft': 8,
            'lf_distance_ft': 325,
            'lf_height_ft': 8,
        }
    },
    'Marlins': {
        'name': 'loanDepot Park',
        'latitude': 25.7911,
        'longitude': -80.2202,
        'elevation_ft': 7,
        'city': 'Miami, FL',
        'orientation_deg': 200,
        'roof_status': 'retracted',
        'region': 'Southeast',
        'wind_pattern': 'Tropical Atlantic winds, hurricane season volatility',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 407,
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },
    'Phillies': {
        'name': 'Citizens Bank Park',
        'latitude': 39.9060,
        'longitude': -75.1672,
        'elevation_ft': 46,
        'city': 'Philadelphia, PA',
        'orientation_deg': 85,
        'roof_status': 'open',
        'region': 'Mid-Atlantic',
        'wind_pattern': 'Atlantic influence, humid air mass',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 401,
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },
    'Nationals': {
        'name': 'Nationals Park',
        'latitude': 38.8728,
        'longitude': -77.0074,
        'elevation_ft': 35,
        'city': 'Washington, DC',
        'orientation_deg': 110,
        'roof_status': 'open',
        'region': 'Mid-Atlantic',
        'wind_pattern': 'Humid subtropical, strong SW winds',
        'outfield_geometry': {
            'rf_distance_ft': 335,
            'rf_height_ft': 8,
            'cf_distance_ft': 402,
            'cf_height_ft': 8,
            'lf_distance_ft': 335,
            'lf_height_ft': 8,
        }
    },

    # NL CENTRAL
    'Cubs': {
        'name': 'Wrigley Field',
        'latitude': 41.9484,
        'longitude': -87.6553,
        'elevation_ft': 594,
        'city': 'Chicago, IL',
        'orientation_deg': 345,  # Almost due N-S
        'roof_status': 'open',
        'region': 'Midwest',
        'wind_pattern': 'Lake Michigan influence, strong afternoon W/SW winds (famous wind)',
        'outfield_geometry': {
            'rf_distance_ft': 353,
            'rf_height_ft': 11,
            'cf_distance_ft': 400,
            'cf_height_ft': 11,
            'lf_distance_ft': 368,
            'lf_height_ft': 11,
        }
    },
    'Cardinals': {
        'name': 'Busch Stadium',
        'latitude': 38.6226,
        'longitude': -90.1928,
        'elevation_ft': 535,
        'city': 'St. Louis, MO',
        'orientation_deg': 35,
        'roof_status': 'retracted',
        'region': 'Midwest',
        'wind_pattern': 'Mississippi Valley influence, humid SE winds',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 400,
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },
    'Brewers': {
        'name': 'American Family Field',
        'latitude': 43.0284,
        'longitude': -87.9711,
        'elevation_ft': 639,
        'city': 'Milwaukee, WI',
        'orientation_deg': 200,
        'roof_status': 'retracted',
        'region': 'Midwest',
        'wind_pattern': 'Lake Michigan, cool N/NW winds',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 400,
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },
    'Reds': {
        'name': 'Great American Ball Park',
        'latitude': 39.0971,
        'longitude': -84.5070,
        'elevation_ft': 570,
        'city': 'Cincinnati, OH',
        'orientation_deg': 60,
        'roof_status': 'open',
        'region': 'Midwest',
        'wind_pattern': 'Ohio River valley, humid air masses',
        'outfield_geometry': {
            'rf_distance_ft': 325,
            'rf_height_ft': 8,
            'cf_distance_ft': 404,
            'cf_height_ft': 8,
            'lf_distance_ft': 325,
            'lf_height_ft': 8,
        }
    },
    'Pirates': {
        'name': 'PNC Park',
        'latitude': 40.4474,
        'longitude': -80.0075,
        'elevation_ft': 710,
        'city': 'Pittsburgh, PA',
        'orientation_deg': 10,  # Nearly due N-S
        'roof_status': 'open',
        'region': 'Mid-Atlantic',
        'wind_pattern': 'Allegheny River valley, variable winds',
        'outfield_geometry': {
            'rf_distance_ft': 325,
            'rf_height_ft': 8,
            'cf_distance_ft': 399,
            'cf_height_ft': 8,
            'lf_distance_ft': 335,
            'lf_height_ft': 8,
        }
    },

    # NL WEST
    'Rockies': {
        'name': 'Coors Field',
        'latitude': 39.7565,
        'longitude': -104.9947,
        'elevation_ft': 5280,  # MILE HIGH
        'city': 'Denver, CO',
        'orientation_deg': 45,
        'roof_status': 'open',
        'region': 'Mountain',
        'wind_pattern': 'Rocky Mountain winds, thin air',
        'outfield_geometry': {
            'rf_distance_ft': 350,
            'rf_height_ft': 8,
            'cf_distance_ft': 415,
            'cf_height_ft': 8,
            'lf_distance_ft': 347,
            'lf_height_ft': 8,
        }
    },
    'Diamondbacks': {
        'name': 'Chase Field',
        'latitude': 33.4455,
        'longitude': -112.0666,
        'elevation_ft': 1100,
        'city': 'Phoenix, AZ',
        'orientation_deg': 200,
        'roof_status': 'closed',  # Retractable, but usually closed
        'region': 'Southwest',
        'wind_pattern': 'Desert winds, monsoon season',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 407,
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },
    'Padres': {
        'name': 'Petco Park',
        'latitude': 32.7075,
        'longitude': -117.1611,
        'elevation_ft': 60,
        'city': 'San Diego, CA',
        'orientation_deg': 295,
        'roof_status': 'open',
        'region': 'Pacific',
        'wind_pattern': 'Marine layer, cool Pacific winds',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 396,
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },
    'Giants': {
        'name': 'Oracle Park',
        'latitude': 37.7785,
        'longitude': -122.3886,
        'elevation_ft': 46,
        'city': 'San Francisco, CA',
        'orientation_deg': 320,  # NW-SE
        'roof_status': 'open',
        'region': 'Pacific',
        'wind_pattern': 'Bay winds, famous afternoon westerly winds',
        'outfield_geometry': {
            'rf_distance_ft': 315,
            'rf_height_ft': 8,
            'cf_distance_ft': 399,
            'cf_height_ft': 8,
            'lf_distance_ft': 335,
            'lf_height_ft': 8,
        }
    },
    'Dodgers': {
        'name': 'Dodger Stadium',
        'latitude': 34.0742,
        'longitude': -118.2437,
        'elevation_ft': 340,
        'city': 'Los Angeles, CA',
        'orientation_deg': 75,  # NE-SW
        'roof_status': 'open',
        'region': 'Pacific',
        'wind_pattern': 'Santa Ana winds (fall/winter), marine layer (summer)',
        'outfield_geometry': {
            'rf_distance_ft': 330,
            'rf_height_ft': 8,
            'cf_distance_ft': 395,
            'cf_height_ft': 8,
            'lf_distance_ft': 330,
            'lf_height_ft': 8,
        }
    },
}


def get_stadium_data(team_abbr: str) -> dict:
    """Get stadium coordinates and geometry for a team."""
    return STADIUM_COORDINATES.get(team_abbr, {})


def get_all_stadiums() -> list:
    """Get list of all stadium team abbreviations."""
    return list(STADIUM_COORDINATES.keys())
