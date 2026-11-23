"""Τα .ps1 με ελληνικά χρειάζονται UTF-8 με BOM: αλλιώς τα Windows PowerShell 5.1
τα διαβάζουν ως ANSI και αποτυγχάνουν στο parsing (23/11/2026, maintenance_gui_live_test.ps1)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Ps1EncodingTests(unittest.TestCase):
    def test_non_ascii_ps1_files_have_utf8_bom(self):
        for path in sorted(ROOT.rglob("*.ps1")):
            if ".venv" in path.parts:
                continue
            data = path.read_bytes()
            if any(byte > 0x7F for byte in data):
                with self.subTest(path=path.name):
                    self.assertTrue(data.startswith(b"\xef\xbb\xbf"), f"{path} χωρίς BOM")


if __name__ == "__main__":
    unittest.main()
