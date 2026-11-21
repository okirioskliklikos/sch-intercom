"""Last Manager-supplied room/name hints for the next enrollment, never authority."""
import json
import os
from pathlib import Path
import tempfile

from shared.identity import ROOM_PATTERN


def load_hints(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"room", "name"}:
            return {}
        room, name = data["room"], data["name"]
        if not isinstance(room, str) or not ROOM_PATTERN.fullmatch(room):
            return {}
        if not isinstance(name, str) or len(name) > 64:
            return {}
        return {"room": room, "name": name}
    except (FileNotFoundError, UnicodeError, ValueError, OSError):
        return {}


def save_hints(path: Path, room: str, name: str) -> None:
    from shared.audio import encode_client_settings
    encode_client_settings(room, name)  # Reject invalid/unbounded metadata.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"room": room, "name": name}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
