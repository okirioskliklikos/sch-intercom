"""4.4 βήμα 2: φορητό zip του βοηθού δοκιμής (χωρίς δίκτυο)."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import zipfile

from tools import make_portable as mp

EMBED_PTH = "python313.zip\n.\n\n# Uncomment to run site.main() automatically\n#import site\n"


def fake_embed() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("python.exe", b"MZ")
        archive.writestr("python313._pth", EMBED_PTH)
        archive.writestr("python313.zip", b"")
    return buffer.getvalue()


class PortableTests(unittest.TestCase):
    def test_pth_adds_packages_app_and_site(self):
        text = mp.patch_pth(EMBED_PTH)
        self.assertEqual(text.splitlines(), ["python313.zip", ".", "Lib\\site-packages",
                                             "..\\app", "import site"])
        self.assertEqual(mp.patch_pth(text), text)          # ξανά: ίδιο αποτέλεσμα

    def test_versions_fall_back_within_branch(self):
        self.assertEqual(mp.candidate_versions("3.13.2"), ["3.13.2", "3.13.1", "3.13.0"])
        existing = {mp.embed_url("3.13.9")}
        self.assertEqual(mp.find_embed("3.13.17", exists=lambda url: url in existing), "3.13.9")
        with self.assertRaises(SystemExit):
            mp.find_embed("3.13.1", exists=lambda url: False)

    def test_code_files_cover_helper_and_skip_tests(self):
        files = {path.as_posix() for path in mp.code_files()}
        for needed in ("tools/network_live_test.py", "tools/network_metrics.py",
                       "tools/failure_live_test.py", "tools/mtls_live_test.py",
                       "tools/manager_role_live_test.py", "client/main.py",
                       "shared/discovery.py", "director/main.py"):
            self.assertIn(needed, files)
        self.assertFalse(any(name.startswith("tests/") or "__pycache__" in name
                             for name in files))

    def test_bat_is_ascii_and_runs_helper(self):
        mp.RUN_CLIENT_BAT.encode("ascii")
        self.assertIn(r'"%~dp0python\python.exe" "%~dp0app\tools\network_live_test.py" client %*',
                      mp.RUN_CLIENT_BAT)

    def test_build_layout(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            embed = folder / "embed.zip"
            embed.write_bytes(fake_embed())

            def fake_install(target, version):
                target.mkdir(parents=True, exist_ok=True)
                (target / "sounddevice.py").write_text("")
                self.assertEqual(version, "3.13.5")

            with mock.patch.object(mp, "install_packages", fake_install):
                result = mp.build(folder / "out", python_version="3.13.5", embed_zip=embed,
                                  report=lambda *_: None)
            with zipfile.ZipFile(result) as archive:
                names = set(archive.namelist())
                pth = archive.read("sch-intercom-client/python/python313._pth").decode()
                readme = archive.read("sch-intercom-client/README.txt")
                bat = archive.read("sch-intercom-client/run-client.bat")
        self.assertIn("sch-intercom-client/python/python.exe", names)
        self.assertIn("sch-intercom-client/python/Lib/site-packages/sounddevice.py", names)
        self.assertIn("sch-intercom-client/app/tools/network_live_test.py", names)
        self.assertIn("..\\app", pth)
        self.assertTrue(readme.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", bat)
        self.assertFalse(any("__pycache__" in name for name in names))


class AskHostTests(unittest.TestCase):
    def test_prompt_only_on_console(self):
        from tools.network_live_test import ask_director_host
        self.assertEqual(ask_director_host(read=lambda _: " 10.1.2.3 ", interactive=True),
                         "10.1.2.3")
        self.assertIsNone(ask_director_host(read=lambda _: "", interactive=True))
        self.assertIsNone(ask_director_host(read=lambda _: "x", interactive=False))

    def test_eof_means_keep_searching(self):
        from tools.network_live_test import ask_director_host

        def closed(_):
            raise EOFError

        self.assertIsNone(ask_director_host(read=closed, interactive=True))


if __name__ == "__main__":
    unittest.main()
