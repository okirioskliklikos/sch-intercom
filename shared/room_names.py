"""5C2: σύντομη εμφάνιση αιθουσών της τρέχουσας σχολικής χρονιάς (χωρίς Qt).

Αίθουσες με όνομα ``C<ΕΕΕΕ>-<υπόλοιπο>`` (π.χ. «C2627-Γ1» = σχολική χρονιά 2026-27,
τμήμα Γ1) εμφανίζονται ως «Γ1» όταν ΕΕΕΕ είναι η τρέχουσα σχολική χρονιά, που αρχίζει
την 1η Σεπτεμβρίου (απόφαση χρήστη 28/11/2026). Παλιότερης/άλλης χρονιάς ή άλλης μορφής:
ολόκληρο το όνομα, ώστε να ξεχωρίζουν όσα δεν μετονομάστηκαν στην αρχή της χρονιάς.

Μόνο για εμφάνιση: μητρώο, πρωτόκολλο και φόρμες μετονομασίας/αλλαγής έχουν πάντα το
πλήρες όνομα.
"""
from __future__ import annotations

from datetime import date
import re

SCHOOL_YEAR_START_MONTH = 9          # 1η Σεπτεμβρίου
PATTERN = re.compile(r"^C(\d{4})-(.+)$", re.DOTALL)
#: Η «σημερινή» ημερομηνία· οι δοκιμές την παγώνουν (tests/__init__.py), ώστε τα
#: αναμενόμενα κείμενα να μην αλλάζουν με τη σχολική χρονιά.
_today = date.today


def school_year(today: date | None = None) -> str:
    """«2627» για 1/9/2026 έως 31/8/2027."""
    today = today or _today()
    start = today.year if today.month >= SCHOOL_YEAR_START_MONTH else today.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def short_room(room: str | None, today: date | None = None) -> str:
    if not room:
        return room or ""
    match = PATTERN.match(room)
    if match and match.group(1) == school_year(today) and match.group(2).strip():
        return match.group(2)
    return room


def room_title(room: str | None, name: str | None, today: date | None = None) -> str:
    """«Γ1 · Board1» (ή μόνο η αίθουσα χωρίς όνομα)."""
    shown = short_room(room, today)
    return f"{shown} · {name}" if name else shown


__all__ = ["school_year", "short_room", "room_title"]
