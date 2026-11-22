"""Tokens στυλ του docs/ui-style.md §1 και κοινό stylesheet (QSS).

Το μοναδικό σημείο όπου ορίζονται χρώματα στον κώδικα UI. Οι οθόνες
χρησιμοποιούν μόνο τα ονόματα (π.χ. `PRIMARY`), ποτέ hex τιμές. Νέο χρώμα
μπαίνει πρώτα ως token στο ui-style.md §1 και μετά εδώ· το
tests/test_ui_theme.py ελέγχει ότι τα δύο συμφωνούν.

Το module δεν εισάγει Qt, ώστε να ελέγχεται χωρίς PySide6.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

#: Αρχεία SVG (εικονίδιο εφαρμογής, τικ checkbox, κουκκίδα radio).
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
APP_ICON_SVG = ASSETS_DIR / "app_icon.svg"

# --- Χρώματα (ui-style.md §1) -------------------------------------------
PRIMARY = "#1d5a99"
PRIMARY_LIGHT = "#dde7f4"
PRIMARY_DARK = "#164a80"
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
DIVIDER = "#b3bcc8"

#: Όνομα token όπως στο ui-style.md → τιμή.
TOKENS: dict[str, str] = {
    "primary": PRIMARY,
    "primary_light": PRIMARY_LIGHT,
    "primary_dark": PRIMARY_DARK,
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
    "divider": DIVIDER,
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

# --- Μέγεθος γραμμάτων και κλίμακα (ui-style.md §2) ---------------------
# Βάση = γραμματοσειρά του συστήματος σε points, αλλά όχι κάτω από
# FONT_MIN_PT. Όλες οι διαστάσεις παρακάτω είναι σε pixel του mockup (βάση
# 13px = 9,75pt) και πολλαπλασιάζονται με την ίδια κλίμακα, ώστε μεγαλύτερα
# γράμματα να μη χωρούν σε στενότερα στοιχεία.
FONT_MIN_PT = 12.0
MOCKUP_BASE_PT = 9.75
FONT_SMALL = 0.92        # pill «Προεπισκόπηση»
FONT_NAV = 1.08          # αριστερό μενού
FONT_ROOM = 1.1          # ονόματα αιθουσών
FONT_CARD_TITLE = 1.15   # τίτλος κάρτας
FONT_PAGE_TITLE = 1.4    # τίτλος σελίδας


@dataclass(frozen=True)
class Metrics:
    """Μεγέθη γραμμάτων/στοιχείων για μια βάση γραμματοσειράς (points)."""

    base_pt: float

    @property
    def scale(self) -> float:
        return self.base_pt / MOCKUP_BASE_PT

    def px(self, mockup_px: float) -> int:
        """Διάσταση του mockup (px) στην τρέχουσα κλίμακα."""
        return max(1, math.floor(mockup_px * self.scale + 0.5))

    def font_pt(self, ratio: float = 1.0) -> float:
        return round(self.base_pt * ratio, 2)


def metrics_for(system_pt: float | None) -> Metrics:
    """Βάση από το σύστημα· άγνωστο ή μικρότερο από FONT_MIN_PT → FONT_MIN_PT."""
    if system_pt is None or system_pt <= 0:
        system_pt = FONT_MIN_PT
    return Metrics(max(float(system_pt), FONT_MIN_PT))


# --- Διαστάσεις στο μέγεθος του mockup (ui-style.md §2, §5) --------------
CARD_RADIUS = 8
NAV_RADIUS = 6
CHECK_RADIUS = 3
CARD_PADDING = 13
CARD_SPACING = 14
ROW_HEIGHT = 30
ROOM_COLUMN_WIDTH = 200     # στήλη αιθουσών· το όνομα κόβεται με «…» στην κουκκίδα
ROOM_DOT_GAP = 9
SIDEBAR_WIDTH = 200
LEFT_COLUMN_WIDTH = 290
ANNOUNCE_MIN_WIDTH = 290    # ελάχιστο πλάτος κάρτας «Ανακοίνωση» όταν ανοίγουν στήλες αιθουσών
ANNOUNCE_RING = 176
ANNOUNCE_CIRCLE = 140
ANNOUNCE_ICON = 60
# Κύματα «Live» (ui-style.md §4): δακτύλιοι από το περίγραμμα του κύκλου
# έως ×WAVE_SCALE της διαμέτρου του, που ξεθωριάζουν με ease-out.
WAVE_SCALE = 1.28
WAVE_WIDTH = 2
WAVE_OPACITY = 0.7
WAVE_COUNT = 3
WAVE_PERIOD_MS = 2400
WAVE_DELAY_MS = 800
WAVE_FADE_MS = 300          # εμφάνιση/εξαφάνιση σταθερού δαχτυλιδιού
WAVE_FRAME_MS = 33          # ~30 fps, αρκετό για ομαλή κίνηση
ICON = 16
BRAND_ICON = 22
DOT = 8
METER_BAR = 6
METER_GAP = 4
SCROLLBAR = 8               # πλάτος scroll bar, χωρίς βελάκια
SCROLLBAR_HANDLE_MIN = 32   # ελάχιστο μήκος λαβής
# Σελίδα «Ασφάλεια» (ui-style.md §6)
ACTION_HEIGHT = 32          # κουμπιά Έγκριση/Απόρριψη και πεδίο κωδικού
ACTION_ICON_STROKE = 2.6    # παχύτερη γραμμή εικονιδίων στα κουμπιά ενέργειας
TRASH_ICON_STROKE = 2.1     # ο κάδος με 2.6 γεμίζει· λεπτότερος όπως στο mockup
COMPUTER_INDENT = 18        # εσοχή υπολογιστών μέσα στην κάρτα της αίθουσας
LEFT_COLUMN_MIN = 380       # στήλη «Νέες εγγραφές»: 380–440 (mockup)
LEFT_COLUMN_MAX = 440
ROOM_CARD_MIN = 260         # ελάχιστο πλάτος κάρτας αίθουσας· όσες χωρούν ανά γραμμή
SECTION_GAP = 16            # απόσταση από το διαχωριστικό ανάμεσα στις δύο ενότητες
REQUEST_CARD_SPACING = 10   # απόσταση ανάμεσα στις κάρτες αιτημάτων


def stylesheet(metrics: Metrics | None = None) -> str:
    """Κοινό QSS. Τα widgets ορίζουν μόνο objectName/ιδιότητες, όχι χρώματα."""
    m = metrics or metrics_for(None)
    px = m.px
    # Πλήρως στρογγυλεμένα pill: ακτίνα = μισό ελάχιστο ύψος (Qt αγνοεί μεγαλύτερη).
    pill_radius = (2 * px(6) + px(20) + 2) // 2
    pill_small_radius = (2 * px(2) + px(16) + 2) // 2
    check = (ASSETS_DIR / "check.svg").as_posix()
    radio_dot = (ASSETS_DIR / "radio_dot.svg").as_posix()
    return f"""
QWidget {{
    color: {TEXT};
    font-size: {m.font_pt()}pt;
}}
QMainWindow, #page {{
    background: {WINDOW_BG};
}}
#sidebar {{
    background: {SIDEBAR_BG};
    border-right: 1px solid {CARD_BORDER};
}}
#brand {{
    font-size: {m.font_pt(FONT_CARD_TITLE)}pt;
    font-weight: 600;
}}
#navItem {{
    text-align: left;
    padding: {px(7)}px {px(10)}px;
    border: 1px solid transparent;
    border-radius: {px(NAV_RADIUS)}px;
    background: transparent;
    color: {TEXT};
    font-size: {m.font_pt(FONT_NAV)}pt;
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
    font-size: {m.font_pt(FONT_PAGE_TITLE)}pt;
    font-weight: 600;
}}
#card {{
    background: {CARD_BG};
    border: 1px solid {CARD_BORDER};
    border-radius: {px(CARD_RADIUS)}px;
}}
#cardTitle {{
    font-size: {m.font_pt(FONT_CARD_TITLE)}pt;
    font-weight: 600;
}}
#onlineCount {{
    font-size: {m.font_pt(FONT_CARD_TITLE)}pt;
    font-weight: 600;
    color: {ONLINE};
}}
#muted, #announceHint {{
    color: {TEXT_MUTED};
}}
#liveBadge {{
    color: {LIVE};
    font-weight: 600;
    font-size: {m.font_pt(FONT_CARD_TITLE)}pt;
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
    border-radius: {pill_radius}px;
    padding: {px(6)}px {px(16)}px;
    min-height: {px(20)}px;
}}
#pillSmall {{
    border-radius: {pill_small_radius}px;
    padding: {px(2)}px {px(9)}px;
    min-height: {px(16)}px;
    font-size: {m.font_pt(FONT_SMALL)}pt;
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
    spacing: {px(9)}px;
    background: transparent;
}}
QRadioButton:disabled {{
    color: {TEXT_MUTED};
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: {px(14)}px;
    height: {px(14)}px;
    background: {CARD_BG};
    border: 1.5px solid {CONTROL_BORDER};
}}
QCheckBox::indicator {{
    border-radius: {px(CHECK_RADIUS)}px;
}}
QRadioButton::indicator {{
    border-radius: {px(8)}px;
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
#roomName {{
    font-size: {m.font_pt(FONT_ROOM)}pt;
}}
QScrollBar:vertical {{
    background: {SIDEBAR_BG};
    width: {px(SCROLLBAR)}px;
    margin: 0;
    border: none;
    border-radius: {px(SCROLLBAR) // 2}px;
}}
QScrollBar:horizontal {{
    background: {SIDEBAR_BG};
    height: {px(SCROLLBAR)}px;
    margin: 0;
    border: none;
    border-radius: {px(SCROLLBAR) // 2}px;
}}
QScrollBar::handle:vertical {{
    background: {PRIMARY};
    min-height: {px(SCROLLBAR_HANDLE_MIN)}px;
    border-radius: {px(SCROLLBAR) // 2}px;
}}
QScrollBar::handle:horizontal {{
    background: {PRIMARY};
    min-width: {px(SCROLLBAR_HANDLE_MIN)}px;
    border-radius: {px(SCROLLBAR) // 2}px;
}}
QScrollBar::handle:hover, QScrollBar::handle:pressed {{
    background: {PRIMARY_DARK};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0;
    height: 0;
    border: none;
    background: none;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}
#tab {{
    padding: {px(6)}px {px(16)}px;
    border: 1px solid {CARD_BORDER};
    border-radius: {px(NAV_RADIUS)}px;
    background: {CARD_BG};
    color: {TEXT_MUTED};
}}
#tab:checked {{
    background: {PRIMARY_LIGHT};
    border-color: {PRIMARY};
    color: {PRIMARY};
    font-weight: 600;
}}
#small {{
    color: {TEXT_MUTED};
    font-size: {m.font_pt(FONT_SMALL)}pt;
}}
#sectionCount {{
    color: {TEXT_MUTED};
    font-size: {m.font_pt(FONT_SMALL)}pt;
}}
#sectionCount[warn="true"] {{
    color: {LIVE};
}}
#divider {{
    background: {DIVIDER};
    border: none;
}}
#requestTitle[final="true"] {{
    color: {TEXT_MUTED};
}}
#requestTitle, #roomTitle {{
    font-weight: 600;
}}
#roomTitle {{
    font-size: {m.font_pt(FONT_ROOM)}pt;
}}
#separator {{
    background: {CARD_BORDER};
    border: none;
}}
#codeInput {{
    background: {CARD_BG};
    border: 1px solid {CONTROL_BORDER};
    border-radius: {px(NAV_RADIUS)}px;
    padding: 0 {px(6)}px;
    min-height: {px(ACTION_HEIGHT) - 2}px;
    max-height: {px(ACTION_HEIGHT) - 2}px;
}}
#codeInput:focus {{
    border: 2px solid {PRIMARY};
}}
#approve, #deny {{
    border-radius: {px(NAV_RADIUS)}px;
    padding: 0 {px(14)}px;
    min-height: {px(ACTION_HEIGHT) - 2}px;
    max-height: {px(ACTION_HEIGHT) - 2}px;
}}
#approve {{
    background: {CONNECTED};
    border: 1px solid {CONNECTED};
    color: {WHITE};
}}
#approve:hover {{
    background: {ONLINE};
    border-color: {ONLINE};
}}
#approve:disabled {{
    background: {CARD_BORDER};
    border-color: {CARD_BORDER};
    color: {TEXT_MUTED};
}}
#deny {{
    background: {CARD_BG};
    border: 1px solid {CONTROL_BORDER};
    color: {LIVE};
}}
#deny:hover {{
    background: {LIVE_LIGHT};
}}
#result {{
    font-size: {m.font_pt(FONT_SMALL)}pt;
    color: {LIVE};
}}
#result[ok="true"] {{
    color: {CONNECTED};
}}
#rowMenu {{
    border: none;
    background: transparent;
    color: {TEXT_MUTED};
    border-radius: {px(NAV_RADIUS)}px;
    padding: 0 {px(8)}px;
    min-height: {px(26)}px;
}}
#rowMenu:hover, #rowMenu:pressed {{
    background: {WINDOW_BG};
}}
#rowMenu::menu-indicator {{
    width: 0;
    image: none;
}}
QScrollArea, #roomGrid, #securityBody {{
    background: transparent;
    border: none;
}}
"""
