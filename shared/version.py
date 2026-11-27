"""Έκδοση εφαρμογής (Φάση 5, docs/phase5.md §4.1).

Μία έκδοση για όλο το repository, μορφή X.Y.Z. Ανεβαίνει σε κάθε έκδοση που
εγκαθίσταται ή διανέμεται σε Clients. Ανεξάρτητη από το CONNECTION_PROTOCOL.
"""
import re

APP_VERSION = "1.0.0"
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$")


def parse_version(text: str) -> tuple[int, int, int]:
    match = VERSION_PATTERN.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"Μη έγκυρη έκδοση: {text!r} (μορφή X.Y.Z)")
    return tuple(int(part) for part in match.groups())
