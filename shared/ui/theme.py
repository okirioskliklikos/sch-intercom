"""Tokens στυλ του docs/ui-style.md §1 και κοινό stylesheet (QSS).

Το μοναδικό σημείο όπου ορίζονται χρώματα στον κώδικα UI. Οι οθόνες
χρησιμοποιούν μόνο τα ονόματα (π.χ. `PRIMARY`), ποτέ hex τιμές. Νέο χρώμα
μπαίνει πρώτα ως token στο ui-style.md §1 και μετά εδώ· το
tests/test_ui_theme.py ελέγχει ότι τα δύο συμφωνούν.

Το module δεν εισάγει Qt, ώστε να ελέγχεται χωρίς PySide6.
"""
from __future__ import annotations

from pathlib import Path

#: Αρχεία SVG (εικονίδιο εφαρμογής, τικ checkbox, κουκκίδα radio).
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
APP_ICON_SVG = ASSETS_DIR / "app_icon.svg"

# --- Χρώματα (ui-style.md §1) -------------------------------------------
PRIMARY = "#1d5a99"
PRIMARY_LIGHT = "#dde7f4"
WINDOW_BG = "#f3f5f9"
SIDEBAR_BG = "#e6ebf3"
CARD_BG = "#ffffff"
CARD_BORDER = "#dde3ec"
TEXT = "#1f2937"
TEXT_MUTED = "#5b6575"
CONTROL_BORDER = "#8a96a6"
ONLINE = "#2e9e4f"
CONNECTED = "#2e8b57"
LIVE = "#c62828"
LIVE_LIGHT = "#f6d5d5"
APP_ICON = "#0e7490"

#: Όνομα token όπως στο ui-style.md → τιμή.
TOKENS: dict[str, str] = {
    "primary": PRIMARY,
    "primary_light": PRIMARY_LIGHT,
    "window_bg": WINDOW_BG,
    "sidebar_bg": SIDEBAR_BG,
    "card_bg": CARD_BG,
    "card_border": CARD_BORDER,
    "text": TEXT,
    "text_muted": TEXT_MUTED,
    "control_border": CONTROL_BORDER,
    "online": ONLINE,
    "connected": CONNECTED,
    "live": LIVE,
    "live_light": LIVE_LIGHT,
    "app_icon": APP_ICON,
}

# Λευκό σε έγχρωμο φόντο (τικ, εικονίδιο μικροφώνου) δεν είναι token.
WHITE = "#ffffff"

# --- Ρόλοι (ui-style.md §2–§4) ------------------------------------------
# Κουκκίδα αίθουσας: online αν έστω ένας Client της είναι online, αλλιώς γκρι.
ROOM_DOT_ONLINE = ONLINE
ROOM_DOT_OFFLINE = CONTROL_BORDER
METER_ON = ONLINE
METER_OFF = CARD_BORDER
# Κουμπί ανακοίνωσης ανενεργό (αναμονή με Mute): χωρίς νέο χρώμα.
DISABLED_FILL = TEXT_MUTED
DISABLED_RING = CARD_BORDER

# --- Διαστάσεις (ui-style.md §2, §5) ------------------------------------
CARD_RADIUS = 8
NAV_RADIUS = 6
CHECK_RADIUS = 3
CARD_PADDING = 13
CARD_SPACING = 14
ROW_HEIGHT = 30
SIDEBAR_WIDTH = 200
LEFT_COLUMN_WIDTH = 290
FONT_BASE_PX = 13
FONT_CARD_TITLE_PX = 15
FONT_PAGE_TITLE_PX = 18


def stylesheet() -> str:
    """Κοινό QSS. Τα widgets ορίζουν μόνο objectName/ιδιότητες, όχι χρώματα."""
    check = (ASSETS_DIR / "check.svg").as_posix()
    radio_dot = (ASSETS_DIR / "radio_dot.svg").as_posix()
    return f"""
QWidget {{
    color: {TEXT};
    font-size: {FONT_BASE_PX}px;
}}
QMainWindow, #page {{
    background: {WINDOW_BG};
}}
#sidebar {{
    background: {SIDEBAR_BG};
    border-right: 1px solid {CARD_BORDER};
}}
#brand {{
    font-size: {FONT_CARD_TITLE_PX}px;
    font-weight: 600;
}}
#navItem {{
    text-align: left;
    padding: 7px 10px;
    border: 1px solid transparent;
    border-radius: {NAV_RADIUS}px;
    background: transparent;
    color: {TEXT};
    font-size: 14px;
}}
#navItem:checked {{
    background: {PRIMARY_LIGHT};
    border-color: {PRIMARY};
    color: {PRIMARY};
    font-weight: 600;
}}
#navItem:disabled {{
    color: {TEXT_MUTED};
}}
#pageTitle {{
    font-size: {FONT_PAGE_TITLE_PX}px;
    font-weight: 600;
}}
#card {{
    background: {CARD_BG};
    border: 1px solid {CARD_BORDER};
    border-radius: {CARD_RADIUS}px;
}}
#cardTitle {{
    font-size: {FONT_CARD_TITLE_PX}px;
    font-weight: 600;
}}
#onlineCount {{
    font-size: {FONT_CARD_TITLE_PX}px;
    font-weight: 600;
    color: {ONLINE};
}}
#muted, #announceHint {{
    color: {TEXT_MUTED};
}}
#announceHint[state="live"] {{
    color: {LIVE};
    font-weight: 700;
}}
#announceHint[state="error"] {{
    color: {LIVE};
}}
#connection {{
    color: {CONNECTED};
    font-weight: 600;
}}
#connection[state="down"] {{
    color: {LIVE};
}}
#pill, #pillSmall {{
    background: {CARD_BG};
    border: 1px solid {CONTROL_BORDER};
    color: {TEXT};
}}
#pill {{
    border-radius: 17px;
    padding: 6px 16px;
    min-height: 20px;
}}
#pillSmall {{
    border-radius: 10px;
    padding: 2px 9px;
    min-height: 16px;
    font-size: 12px;
}}
#pill:checked, #pillSmall:checked {{
    background: {PRIMARY_LIGHT};
    border-color: {PRIMARY};
    color: {PRIMARY};
    font-weight: 600;
}}
#pill:disabled, #pillSmall:disabled {{
    color: {TEXT_MUTED};
    border-color: {CARD_BORDER};
}}
QCheckBox, QRadioButton {{
    spacing: 9px;
    background: transparent;
}}
QRadioButton:disabled {{
    color: {TEXT_MUTED};
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    background: {CARD_BG};
    border: 1.5px solid {CONTROL_BORDER};
}}
QCheckBox::indicator {{
    border-radius: {CHECK_RADIUS}px;
}}
QRadioButton::indicator {{
    border-radius: 8px;
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {PRIMARY};
    border-color: {PRIMARY};
}}
QCheckBox::indicator:checked {{
    image: url("{check}");
}}
QRadioButton::indicator:checked {{
    image: url("{radio_dot}");
}}
QCheckBox::indicator:unchecked:disabled, QRadioButton::indicator:unchecked:disabled {{
    border-color: {CARD_BORDER};
}}
#roomRow {{
    border-bottom: 1px solid {CARD_BORDER};
}}
QScrollArea, #roomGrid {{
    background: transparent;
    border: none;
}}
"""
