"""
Global configuration for Premium Short Straddle.

All strategy constants should be defined here.
Avoid hardcoding values anywhere else in the project.
"""

# ==========================================================
# INDEX
# ==========================================================

INDEX = "SENSEX"

LOT_SIZE = 20


# ==========================================================
# MARKET TIMINGS
# ==========================================================

ENTRY_TIME = "09:50"

EXIT_TIME = "15:20"


# ==========================================================
# PREMIUM SELECTION
# ==========================================================

# Target premium to sell
TARGET_PREMIUM = 60

# Allowed range
PREMIUM_MIN = 55
PREMIUM_MAX = 65


# ==========================================================
# RISK MANAGEMENT
# ==========================================================

# Stop loss = Entry × (1 + STOPLOSS_PERCENT/100)
STOPLOSS_PERCENT = 50

# Maximum re-entry per leg
MAX_REENTRY = 1


# ==========================================================
# ENGINE
# ==========================================================

# LTP refresh interval (seconds)
LTP_REFRESH_INTERVAL = 1

# Order retries
ORDER_RETRY_COUNT = 3

# Seconds to wait for order completion
ORDER_STATUS_TIMEOUT = 5


# ==========================================================
# DASHBOARD
# ==========================================================

REFRESH_INTERVAL = 1


# ==========================================================
# DRY RUN
# ==========================================================

DEFAULT_DRY_RUN = True
