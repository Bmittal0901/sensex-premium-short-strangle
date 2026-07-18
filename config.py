"""
Global configuration for Premium Short Straddle.

All strategy constants should be defined here.
Avoid hardcoding values anywhere else in the project.
"""
INDEX = "SENSEX"
LOT_SIZE = 20
ENTRY_TIME = "09:50"
EXIT_TIME = "15:20"
TARGET_PREMIUM = 60
PREMIUM_MIN = 55
PREMIUM_MAX = 65
STOPLOSS_PERCENT = 50

MAX_REENTRY = 1

LTP_REFRESH_INTERVAL = 1

ORDER_RETRY_COUNT = 3

ORDER_STATUS_TIMEOUT = 5

REFRESH_INTERVAL = 1

DEFAULT_DRY_RUN = True

import os

def env_dry_run():
    return os.getenv("DRY_RUN", "true").lower() == "true"

TARGET_PREMIUM = 60

PREMIUM_MIN = 55
PREMIUM_MAX = 65

FALLBACK_PREMIUM_MIN = 50
FALLBACK_PREMIUM_MAX = 70
FALLBACK_PREMIUM_TOLERANCE = 10