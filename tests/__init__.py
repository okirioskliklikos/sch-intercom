"""Automated tests for the initial School Intercom prototype."""

# 5C2: οι αίθουσες της τρέχουσας σχολικής χρονιάς εμφανίζονται σύντομα («C2627-Γ1» → «Γ1»).
# Τα tests γράφουν «C2627-Γ1» ως πλήρες όνομα· σταθερή ημερομηνία εκτός 2026-27, ώστε τα
# κείμενα να μην αλλάζουν με το ημερολόγιο. Η σύντομη μορφή ελέγχεται ρητά
# (tests/test_room_names.py) με δική της ημερομηνία.
from datetime import date as _date  # noqa: E402

from shared import room_names as _room_names  # noqa: E402

_room_names._today = lambda: _date(2031, 1, 15)
