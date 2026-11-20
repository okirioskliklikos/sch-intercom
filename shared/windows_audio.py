"""Κοινός κώδικας έντασης Windows (Core Audio μέσω pycaw).

Χρησιμοποιείται από τον Client (client/windows_volume.py) και από το
διαγνωστικό script tools/windows_audio_check.py. Οι βιβλιοθήκες Windows
εισάγονται μόνο όταν δημιουργείται PycawMaster, ώστε το module να φορτώνει και
σε Linux (tests).
"""

from __future__ import annotations

import time
from typing import Callable

VOLUME_TOLERANCE = 0.01  # 1 % ανοχή στη σύγκριση έντασης (στρογγυλοποιήσεις Windows)
DEFAULT_RAMP_MS = 150  # σταδιακή αλλαγή έντασης αντί για απότομο βήμα («κλικ»)
RAMP_STEP_MS = 10

Sleep = Callable[[float], None]


def same_volume(a: float, b: float) -> bool:
    return abs(a - b) <= VOLUME_TOLERANCE


def ramp_volume(set_volume: Callable[[float], None], start: float, end: float,
                ramp_ms: float, sleep: Sleep = time.sleep) -> None:
    """Αλλάζει την ένταση σταδιακά από start σε end μέσα σε ramp_ms.

    Με ramp_ms <= 0 η αλλαγή γίνεται με ένα βήμα (όπως στην αρχική έκδοση).
    """
    steps = max(1, int(ramp_ms // RAMP_STEP_MS)) if ramp_ms > 0 else 1
    for i in range(1, steps + 1):
        set_volume(end if i == steps else start + (end - start) * i / steps)
        if i < steps:
            sleep(ramp_ms / steps / 1000)


class PycawMaster:
    def __init__(self) -> None:
        from pycaw.pycaw import AudioUtilities

        speakers = AudioUtilities.GetSpeakers()
        self.device_name = getattr(speakers, "FriendlyName", None)
        endpoint = getattr(speakers, "EndpointVolume", None)
        if endpoint is None:
            # Παλαιότερες εκδόσεις pycaw: το GetSpeakers επιστρέφει IMMDevice.
            from ctypes import POINTER, cast

            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import IAudioEndpointVolume

            interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            endpoint = cast(interface, POINTER(IAudioEndpointVolume))
            if self.device_name is None:
                try:
                    self.device_name = AudioUtilities.CreateDevice(speakers).FriendlyName
                except Exception:  # noqa: BLE001
                    self.device_name = "(άγνωστο όνομα)"
        self._endpoint = endpoint

    def get_volume(self) -> float:
        return float(self._endpoint.GetMasterVolumeLevelScalar())

    def set_volume(self, value: float) -> None:
        self._endpoint.SetMasterVolumeLevelScalar(float(value), None)

    def get_mute(self) -> bool:
        return bool(self._endpoint.GetMute())

    def set_mute(self, muted: bool) -> None:
        self._endpoint.SetMute(1 if muted else 0, None)
