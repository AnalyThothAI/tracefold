"""The market read surface's own bounds and its two status vocabularies.

A value module for the same reason `oi_contracts.py` is one: the HTTP route validates a window
against these numbers, the storage statements bound their own scans with them, and the News package
surface re-exports them for App composition. One definition, three readers, no import of a
persistence or runtime owner.

`notification_status` is deliberately not a member of `parse_status`. A raw card that was delivered
and a parsed card that was not are both ordinary outcomes, and one combined column would have to
misreport one of them.
"""

from typing import Final

# One page of collapsed groups.
MARKET_PAGE_MAX: Final = 100
# The default window a reader gets without asking, and the widest span one request may cover. The
# span bounds what a single page may scan; it says nothing about how far back the data goes, and any
# window inside the retention is readable.
MARKET_WINDOW_DEFAULT_MS: Final = 72 * 60 * 60_000
MARKET_WINDOW_MAX_MS: Final = 168 * 60 * 60_000
# What one page may read inside that window. Collapsing consecutive observations is a property of the
# whole window rather than of a page -- otherwise one group would appear twice, with two different
# counts, either side of a page boundary -- so the window is read and the groups are paged out of it.
# At the measured 208 market observations a day a full 168 h window is about 1 500 rows.
MARKET_WINDOW_ROW_CAP: Final = 5_000
# One group's expanded timeline on the detail page.
MARKET_TIMELINE_MAX: Final = 200

# PR-1 of #553 stores and reads market facts; the notification loop is PR-2's. Saying so in a field
# the reader can see is the honest form of "not wired yet" -- the alternative is a page that looks
# like it weighed the observation and decided not to send it.
NOTIFICATION_STATUS_NOT_CONNECTED: Final = "not_connected"
NOTIFICATION_REASON_NOT_CONNECTED: Final = "market_notifications_not_connected"

__all__ = [
    "MARKET_PAGE_MAX",
    "MARKET_TIMELINE_MAX",
    "MARKET_WINDOW_DEFAULT_MS",
    "MARKET_WINDOW_MAX_MS",
    "MARKET_WINDOW_ROW_CAP",
    "NOTIFICATION_REASON_NOT_CONNECTED",
    "NOTIFICATION_STATUS_NOT_CONNECTED",
]
