"""
premium_strategy.py

Pure strategy functions for the premium-selling algorithm.

Rules:
- Sell CE and PE independently.
- Target premium = 60.
- Preferred band = 60 ± 5.
- Fallback band = 60 ± 10.
- Stop loss = 150% of entry premium.
- One re-entry on the SAME strike.
"""

PREMIUM_TARGET = 60
PREMIUM_TOLERANCE = 5
STOPLOSS_MULTIPLIER = 1.5


def in_premium_band(premium, target=PREMIUM_TARGET, tolerance=PREMIUM_TOLERANCE):
    """
    Returns True if premium lies inside the specified band.
    """
    return (target - tolerance) <= premium <= (target + tolerance)


def pick_strike_in_band(candidates, target=PREMIUM_TARGET, tolerance=PREMIUM_TOLERANCE):
    """
    candidates = [
        {
            "symbol": "...",
            "strike": ...,
            "token": ...,
            "ltp": ...
        },
        ...
    ]

    Returns the strike whose premium is inside the band and closest to target.
    """

    valid = [
        c for c in candidates
        if in_premium_band(c["ltp"], target, tolerance)
    ]

    if not valid:
        return None

    return min(
        valid,
        key=lambda x: abs(x["ltp"] - target)
    )


def sell_leg_hit_stop_loss(entry_premium, current_premium):
    """
    Stop loss for short option.

    Entry 60
    SL = 90
    """

    return current_premium >= entry_premium * STOPLOSS_MULTIPLIER


def sell_leg_pnl(entry_premium, exit_premium, qty):
    """
    Positive = profit
    Negative = loss
    """

    return (entry_premium - exit_premium) * qty


def combined_pnl(legs):
    """
    Total PnL =
        realized pnl
      + unrealized pnl
    """

    total = 0

    for leg in legs.values():

        total += leg.get("realized_pnl", 0)

        if (
            leg["phase"] == "in_position"
            and leg["entry_premium"] is not None
            and leg["current_premium"] is not None
        ):
            total += sell_leg_pnl(
                leg["entry_premium"],
                leg["current_premium"],
                leg["qty"]
            )

    return total