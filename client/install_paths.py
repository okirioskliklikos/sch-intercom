"""Φάκελοι εγκατεστημένου Client (Φάση 5A, docs/phase5.md §3).

Πρόγραμμα: %LOCALAPPDATA%\\sch-intercom (σβήνεται στην απεγκατάσταση).
Δεδομένα:  %USERPROFILE%\\.school-intercom (μένουν· μόνο η «Πλήρης αφαίρεση» τα σβήνει).

Για δοκιμές και για μη τυπικές εγκαταστάσεις οι φάκελοι αλλάζουν με
SCH_INTERCOM_HOME / SCH_INTERCOM_DATA ή με το install.json του προγράμματος.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

from shared.version import parse_version

HOME_ENV = "SCH_INTERCOM_HOME"
DATA_ENV = "SCH_INTERCOM_DATA"
CURRENT_FILE = "current.json"
INSTALL_FILE = "install.json"
SETTINGS_FILE = "client-settings.json"
MANAGER_CERT = "manager-ca.pem"
CLI_NAME = "sch-intercom-client.cmd"
DEFAULT_RUN_NAME = "SchoolIntercomClient"
#: Ο κώδικας του repository (εκτέλεση χωρίς εγκατάσταση: ανάπτυξη και tests).
SOURCE_ROOT = Path(__file__).resolve().parents[1]


def default_home() -> Path:
    if os.environ.get(HOME_ENV):
        return Path(os.environ[HOME_ENV])
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "sch-intercom"
    return Path.home() / ".local" / "share" / "sch-intercom"


def data_dir() -> Path:
    """Φάκελος ταυτότητας/ρυθμίσεων Client (ίδιος με πριν από τη Φάση 5)."""
    if os.environ.get(DATA_ENV):
        return Path(os.environ[DATA_ENV])
    return Path.home() / ".school-intercom"


def atomic_write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"Μη αναγνώσιμο {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Μη έγκυρο {path}")
    return data


def load_settings(folder: Path | None = None) -> dict:
    """Τοπικές ρυθμίσεις που αλλάζουν μόνο από τη γραμμή εντολών (§5.5)."""
    data = _read_json(Path(folder or data_dir()) / SETTINGS_FILE) or {}
    host = data.get("director_host")
    device = data.get("device")
    return {"director_host": host if isinstance(host, str) and host else None,
            "device": device if type(device) is int and device >= 0 else None}


def save_settings(folder: Path | None, **changes) -> dict:
    folder = Path(folder or data_dir())
    settings = load_settings(folder)
    for key, value in changes.items():
        if key not in settings:
            raise KeyError(key)
        settings[key] = value
    atomic_write_json(folder / SETTINGS_FILE, settings)
    return settings


class Layout:
    """Διάταξη φακέλου προγράμματος: versions\\<έκδοση>, current.json, logs."""

    def __init__(self, home: Path | None = None, data: Path | None = None):
        self.home = Path(home) if home is not None else default_home()
        self._data = Path(data) if data is not None else None

    # -- αρχεία -------------------------------------------------------------
    @property
    def current_file(self) -> Path:
        return self.home / CURRENT_FILE

    @property
    def install_file(self) -> Path:
        return self.home / INSTALL_FILE

    @property
    def versions(self) -> Path:
        return self.home / "versions"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def lock_file(self) -> Path:
        return self.home / "supervisor.lock"

    @property
    def key_file(self) -> Path:
        return self.home / "control.key"

    @property
    def cli_file(self) -> Path:
        return self.home / CLI_NAME

    def install_info(self) -> dict:
        return _read_json(self.install_file) or {}

    @property
    def data(self) -> Path:
        """--data-dir > SCH_INTERCOM_DATA > install.json > %USERPROFILE%\\.school-intercom."""
        if self._data is not None:
            return self._data
        if os.environ.get(DATA_ENV):
            return Path(os.environ[DATA_ENV])
        stored = self.install_info().get("data_dir")
        return Path(stored) if isinstance(stored, str) and stored else data_dir()

    def set_install_version(self, version: str) -> None:
        """5B2b: μετά από αυτόματη ενημέρωση το install.json δείχνει τη νέα έκδοση."""
        parse_version(version)
        info = self.install_info()
        if info:
            info["installed_version"] = version
            atomic_write_json(self.install_file, info)

    @property
    def run_name(self) -> str:
        name = self.install_info().get("run_name")
        return name if isinstance(name, str) and name else DEFAULT_RUN_NAME

    # -- current.json ---------------------------------------------------------
    def read_current(self) -> dict | None:
        data = _read_json(self.current_file)
        if data is None:
            return None
        current = data.get("current")
        parse_version(current)
        previous = data.get("previous")
        if previous is not None:
            parse_version(previous)
        failed = data.get("failed", [])
        if not isinstance(failed, list):
            raise ValueError(f"Μη έγκυρο {self.current_file}")
        return {"current": current, "previous": previous,
                "trial": data.get("trial"), "failed": [str(v) for v in failed]}

    def current_version(self) -> str | None:
        state = self.read_current()
        return state["current"] if state else None

    def set_installed(self, version: str) -> dict:
        """Ο εγκαταστάτης έβαλε την έκδοση: γίνεται current, η παλιά previous."""
        parse_version(version)
        state = self.read_current() or {"current": None, "previous": None,
                                         "trial": None, "failed": []}
        if state["current"] != version:
            state["previous"] = state["current"]
            state["current"] = version
        state["trial"] = None
        state["failed"] = [v for v in state["failed"] if v != version]
        atomic_write_json(self.current_file, state)
        return state

    # -- εκτέλεση -------------------------------------------------------------
    def version_dir(self, version: str) -> Path:
        parse_version(version)
        return self.versions / version

    def python(self, version: str | None, *, windowed: bool = False) -> Path:
        """Python της έκδοσης· χωρίς εγκατεστημένη (ανάπτυξη/tests) η τρέχουσα."""
        if version is not None:
            folder = self.version_dir(version) / "python"
            exe = folder / ("pythonw.exe" if windowed else "python.exe")
            if exe.exists():
                return exe
        return Path(sys.executable)

    def child_env(self, version: str | None) -> dict:
        env = dict(os.environ)
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                    "PYTHONUNBUFFERED": "1", HOME_ENV: str(self.home),
                    DATA_ENV: str(self.data)})
        embedded = version is not None and (self.version_dir(version) / "python").exists()
        if not embedded:
            # Ανάπτυξη: ο κώδικας από το repository (το embeddable έχει ..\\app στο ._pth).
            # 5B2b: έκδοση με app\\ χωρίς embeddable Python (δοκιμές Linux) → από το app.
            app = self.version_dir(version) / "app" if version is not None else None
            first = str(app) if app is not None and app.is_dir() else str(SOURCE_ROOT)
            paths = [first] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
            env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
        return env

    def module_command(self, module: str, *args: str, version: str | None = None,
                       windowed: bool = False) -> list[str]:
        if version is None:
            try:
                version = self.current_version()
            except ValueError:
                version = None
        return [str(self.python(version, windowed=windowed)), "-m", module, *args]
