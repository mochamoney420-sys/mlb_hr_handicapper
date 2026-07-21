#!/usr/bin/env python
"""Stadium elevation and location data for density altitude calculations."""

STADIUM_INFO = {
    # AL East
    117: {'name': 'Fenway Park', 'elevation': 21, 'city': 'Boston'},
    139: {'name': 'Rogers Centre', 'elevation': 289, 'city': 'Toronto'},
    133: {'name': 'Oriole Park', 'elevation': 0, 'city': 'Baltimore'},
    142: {'name': 'Yankee Stadium', 'elevation': 5, 'city': 'New York'},
    145: {'name': 'Tropicana Field', 'elevation': 18, 'city': 'St. Petersburg'},
    
    # AL Central
    116: {'name': 'Comerica Park', 'elevation': 645, 'city': 'Detroit'},
    143: {'name': 'Progressive Field', 'elevation': 660, 'city': 'Cleveland'},
    147: {'name': 'Guaranteed Rate Field', 'elevation': 597, 'city': 'Chicago'},
    138: {'name': 'Kauffman Stadium', 'elevation': 750, 'city': 'Kansas City'},
    136: {'name': 'Target Field', 'elevation': 815, 'city': 'Minneapolis'},
    
    # AL West
    108: {'name': 'Globe Life Field', 'elevation': 616, 'city': 'Arlington'},
    140: {'name': 'Oakland Coliseum', 'elevation': 25, 'city': 'Oakland'},
    137: {'name': 'T-Mobile Park', 'elevation': 0, 'city': 'Seattle'},
    146: {'name': 'Angel Stadium', 'elevation': 160, 'city': 'Anaheim'},
    135: {'name': 'Petco Park', 'elevation': 28, 'city': 'San Diego'},
    
    # NL East
    120: {'name': 'Nationals Park', 'elevation': 10, 'city': 'Washington'},
    119: {'name': 'Citi Field', 'elevation': 10, 'city': 'New York'},
    144: {'name': 'Citizens Bank Park', 'elevation': 32, 'city': 'Philadelphia'},
    113: {'name': 'Truist Park', 'elevation': 886, 'city': 'Atlanta'},
    115: {'name': 'loanDepot park', 'elevation': 6, 'city': 'Miami'},
    
    # NL Central
    158: {'name': 'American Family Field', 'elevation': 615, 'city': 'Milwaukee'},
    112: {'name': 'Great American Ball Park', 'elevation': 455, 'city': 'Cincinnati'},
    114: {'name': 'Busch Stadium', 'elevation': 430, 'city': 'St. Louis'},
    141: {'name': 'Wrigley Field', 'elevation': 594, 'city': 'Chicago'},
    134: {'name': 'PNC Park', 'elevation': 730, 'city': 'Pittsburgh'},
    
    # NL West
    104: {'name': 'AT&T Park', 'elevation': 50, 'city': 'San Francisco'},
    110: {'name': 'Chase Field', 'elevation': 1100, 'city': 'Phoenix'},
    127: {'name': 'Dodger Stadium', 'elevation': 395, 'city': 'Los Angeles'},
    129: {'name': 'Coors Field', 'elevation': 5280, 'city': 'Denver'},  # HIGHEST
    117: {'name': 'Rockies', 'elevation': 5280, 'city': 'Denver'},
    111: {'name': 'Minute Maid Park', 'elevation': 22, 'city': 'Houston'},
    118: {'name': 'Tropicana Field', 'elevation': 18, 'city': 'St. Petersburg'},
}


def get_stadium_elevation(venue_id):
    """Get elevation for stadium."""
    return STADIUM_INFO.get(venue_id, {}).get('elevation', 0)


def get_high_altitude_parks():
    """Return parks above 4000 ft elevation (extreme ball carry)."""
    high_alt = {k: v for k, v in STADIUM_INFO.items() if v.get('elevation', 0) > 4000}
    return high_alt


# Coors Field is the extreme: 5280 ft elevation
# Expected +15-20% ball carry vs sea level parks
