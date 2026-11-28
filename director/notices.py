"""4.3C2: κείμενα των κόκκινων ειδοποιήσεων «γεμάτος» και «ίδια ταυτότητα» (χωρίς Qt).

Οι ειδοποιήσεις εμφανίζονται κάτω από τον τίτλο, σε όλες τις σελίδες, όπως του ρόλου
Manager (mockup 4.3C2, απόφαση χρήστη 24/11/2026). Τα δεδομένα έρχονται από τον
`director.broadcast.AudioBroadcaster` (`full_waiting`, `duplicate_identities`).
"""
from __future__ import annotations

from html import escape
from shared.room_names import room_title

#: Πόσοι υπολογιστές σε αναμονή ονομάζονται· οι υπόλοιποι γίνονται «και N ακόμη».
FULL_NAMES_SHOWN = 3
TEXT_DUPLICATE_STATE = "Σε 2 υπολογιστές"
TEXT_SHOW = "Προβολή"
DUPLICATE_HINT = "Στον έναν από τους δύο:"
DUPLICATE_COMMAND = "client --new-identity"
DUPLICATE_HINT_END = "και νέα έγκριση"


def _label(room: str, name: str | None) -> str:
    return room_title(room, name)


def full_html(waiting: list[tuple[str, str, str]], limit: int) -> str | None:
    """«Συμπληρώθηκε το όριο των 70 υπολογιστών. Δεν συνδέονται: Γ3 · Board1, …» ή None."""
    if not waiting:
        return None
    labels = sorted(_label(room, name) for _client_id, room, name in waiting)
    shown = ", ".join(f"<b>{escape(label)}</b>" for label in labels[:FULL_NAMES_SHOWN])
    rest = len(labels) - FULL_NAMES_SHOWN
    if rest > 0:
        shown += f" και {rest} ακόμη"
    return (f"Συμπληρώθηκε το όριο των <b>{limit}</b> υπολογιστών. "
            f"Δεν συνδέονται: {shown}.")


def duplicate_html(duplicates: list[tuple[str, str, tuple[str, ...]]],
                   names: dict[str, str] | None = None, *, mono: str = "monospace",
                   hint_size: str = "small") -> str | None:
    """Ίδια ταυτότητα σε 2 υπολογιστές (αίθουσα · όνομα, IP) και τι κάνει ο χρήστης, ή None.

    ``names``: όνομα υπολογιστή ανά UUID (από το μητρώο ή τη σύνδεση). ``hint_size``:
    μέγεθος της δεύτερης γραμμής (CSS, π.χ. "10.8pt").
    """
    if not duplicates:
        return None
    names = names or {}
    items = "· ".join(
        f"<b>{escape(_label(room, names.get(client_id)))}</b> ({escape(', '.join(addresses))})"
        for client_id, room, addresses in duplicates)
    return (f"Ίδια ταυτότητα σε 2 υπολογιστές: {items}"
            f"<br><span style='font-size:{hint_size}'>{DUPLICATE_HINT} "
            f"<span style='font-family:{mono}'>{DUPLICATE_COMMAND}</span> "
            f"{DUPLICATE_HINT_END}</span>")


def duplicate_tooltip(addresses: tuple[str, ...]) -> str:
    """Tooltip της γραμμής υπολογιστή: «Ίδια ταυτότητα από <b>IP</b> και <b>IP</b>»."""
    shown = [f"<b>{escape(address)}</b>" for address in addresses]
    joined = ", ".join(shown[:-1]) + " και " + shown[-1] if len(shown) > 1 else "".join(shown)
    return f"Ίδια ταυτότητα από {joined}"
