import math


def american_to_decimal(odds):
    odds = int(odds)
    if odds > 0:
        return 1 + odds / 100.0
    return 1 - 100.0 / odds


def american_to_prob(odds):
    odds = int(odds)
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def devig_prob(yes_prob, no_prob):
    total = yes_prob + no_prob
    if total <= 0:
        return (0.0, 0.0)
    return yes_prob / total, no_prob / total


def calc_edge(implied_prob, fair_prob):
    if fair_prob <= 0:
        return 0.0
    return implied_prob - fair_prob


def prob_to_american(prob):
    if prob <= 0:
        return None
    if prob >= 0.5:
        american = -round((prob / (1 - prob)) * 100)
        return f"{american}"
    american = round(((1 - prob) / prob) * 100)
    return f"+{american}"


def format_edge(prob):
    return f"{prob * 100:.1f}%"


def reverse_line_movement(start_odds, end_odds, start_bet_pct, end_bet_pct):
    return (start_odds < end_odds and start_bet_pct > end_bet_pct) or (
        start_odds > end_odds and start_bet_pct < end_bet_pct
    )


def is_sharp_book(book_name, sharp_books=None):
    if sharp_books is None:
        sharp_books = ['draftkings', 'fanduel', 'caesars', 'betmgm', 'pointsbet']
    return book_name.lower() in sharp_books
