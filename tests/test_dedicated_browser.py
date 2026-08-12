import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hackathon_searcher import dedicated_browser
from hackathon_searcher.team import TeamConfig


class DedicatedBrowserTests(unittest.TestCase):
    def test_profile_paths_are_platform_specific(self):
        home = Path("/home/example")
        self.assertEqual(
            dedicated_browser.dedicated_profile_path("windows", {"LOCALAPPDATA": "C:/Local"}, home),
            Path("C:/Local/HackathonSearcher/ChromeProfile"),
        )
        self.assertEqual(
            dedicated_browser.dedicated_profile_path("darwin", {}, home),
            home / "Library/Application Support/HackathonSearcher/ChromeProfile",
        )
        self.assertEqual(
            dedicated_browser.dedicated_profile_path("linux", {}, home),
            home / ".local/share/hackathon-searcher/chrome-profile",
        )

    def test_chrome_detection_uses_chrome_only(self):
        expected = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        found = dedicated_browser.find_chrome("windows", {"PROGRAMFILES": "C:/Program Files"}, exists=lambda path: path == expected)
        self.assertEqual(found, expected)

    def test_missing_chrome_has_human_error(self):
        with patch.object(dedicated_browser, "find_chrome", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Google Chrome was not found"):
                dedicated_browser.chrome_or_error()

    def test_never_accepts_normal_chrome_profile(self):
        normal = dedicated_browser.normal_chrome_data_paths()[0]
        self.assertFalse(dedicated_browser.is_dedicated_profile_safe(normal))

    def test_launch_uses_only_dedicated_user_data_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "isolated-profile"
            chrome = Path(directory) / "chrome"
            with patch.object(dedicated_browser, "chrome_or_error", return_value=chrome), \
                 patch.object(dedicated_browser, "dedicated_profile_path", return_value=profile), \
                 patch.object(dedicated_browser, "is_dedicated_profile_safe", return_value=True), \
                 patch.object(dedicated_browser, "EXTENSION_DIR", Path(directory)), \
                 patch("subprocess.Popen") as launch:
                dedicated_browser.launch_dedicated_chrome(["about:blank"])
            args = launch.call_args.args[0]
            self.assertIn(f"--user-data-dir={profile}", args)
            self.assertNotIn("--load-extension", " ".join(args))

    def test_missing_extension_handshake_is_reported(self):
        with patch.object(dedicated_browser, "HANDSHAKE_PATH", Path(tempfile.gettempdir()) / "missing-handshake.json"):
            self.assertIsNone(dedicated_browser.extension_handshake())

    def test_successful_extension_handshake_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handshake.json"
            path.write_text(json.dumps({"extension_version": "1.2.2", "luma_session": "signed_out"}), encoding="utf-8")
            with patch.object(dedicated_browser, "HANDSHAKE_PATH", path):
                self.assertEqual(dedicated_browser.extension_handshake()["extension_version"], "1.2.2")

    def test_bridge_unavailable_is_visible(self):
        with patch.object(dedicated_browser, "ensure_bridge", return_value=False), patch.object(dedicated_browser, "bridge_connected", return_value=False), patch.object(dedicated_browser, "find_chrome", return_value=None):
            result = dedicated_browser.browser_verify(launch=False)
        self.assertFalse(result["bridge"])

    def test_luma_session_warning_is_exposed(self):
        with patch.object(dedicated_browser, "ensure_bridge", return_value=True), patch.object(dedicated_browser, "bridge_connected", return_value=True), patch.object(dedicated_browser, "find_chrome", return_value=None), patch.object(dedicated_browser, "extension_handshake", return_value={"luma_session": "present"}):
            result = dedicated_browser.browser_verify(launch=False)
        self.assertEqual(result["luma_session"], "present")

    def test_team_sizes_one_two_and_four_are_valid(self):
        for members in (("alex",), ("alex", "sam"), ("alex", "sam", "jordan", "riley")):
            self.assertEqual(TeamConfig.from_dict({"members": list(members)}).members, members)


if __name__ == "__main__":
    unittest.main()
