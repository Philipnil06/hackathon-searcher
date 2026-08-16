"""Dedicated, persistent Chrome profile used only by Hackathon Searcher."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from hackathon_searcher.human_assist_bridge import HOST, PORT

EXTENSION_DIR = Path(__file__).resolve().parent.parent / "chrome_extension"
HANDSHAKE_PATH = Path("human_assist") / "extension_handshake.json"


def dedicated_profile_path(system: str | None = None, env: dict[str, str] | None = None, home: Path | None = None) -> Path:
    """Return the OS-local profile location, never a normal Chrome profile."""
    system = (system or platform.system()).lower()
    env = env or os.environ
    home = home or Path.home()
    if system == "windows":
        root = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        preferred = root / "HackathonSearcher" / "ChromeProfile"
        legacy = root / "HackathonSearcher" / "ChromeGuest"
        # Earlier local versions used ChromeGuest. Reuse it if it is the only
        # existing dedicated profile so its manually loaded extension/session
        # state is preserved without touching normal Chrome data.
        return legacy if legacy.is_dir() and not preferred.exists() else preferred
    if system == "darwin":
        return home / "Library" / "Application Support" / "HackathonSearcher" / "ChromeProfile"
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "hackathon-searcher" / "chrome-profile"


def normal_chrome_data_paths(system: str | None = None, env: dict[str, str] | None = None, home: Path | None = None) -> tuple[Path, ...]:
    system = (system or platform.system()).lower()
    env = env or os.environ
    home = home or Path.home()
    if system == "windows":
        return (Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Google" / "Chrome" / "User Data",)
    if system == "darwin":
        return (home / "Library" / "Application Support" / "Google" / "Chrome",)
    return (home / ".config" / "google-chrome", home / ".config" / "chromium")


def is_dedicated_profile_safe(profile: Path) -> bool:
    """Fail closed if a caller ever points at a regular Chrome data directory."""
    resolved = profile.resolve(strict=False)
    return all(resolved != normal.resolve(strict=False) for normal in normal_chrome_data_paths())


def find_chrome(system: str | None = None, env: dict[str, str] | None = None, exists=None) -> Path | None:
    """Find Google Chrome only; never silently substitute another browser."""
    system = (system or platform.system()).lower()
    env = env or os.environ
    exists = exists or Path.is_file
    if system == "windows":
        candidates = (
            Path(env.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(env.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(env.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        )
    elif system == "darwin":
        candidates = (Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        candidates = (Path("/usr/bin/google-chrome"), Path("/usr/bin/google-chrome-stable"), Path("/snap/bin/chromium"))
    for candidate in candidates:
        if exists(candidate):
            return candidate
    command = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    return Path(command) if command else None


def chrome_or_error() -> Path:
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("Google Chrome was not found. Install Google Chrome, then run `python -m hackathon_searcher.cli browser setup` again.")
    return chrome


def ensure_bridge() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=0.25):
            return True
    except OSError:
        pass
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([sys.executable, "-m", "hackathon_searcher.human_assist_bridge"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    for _ in range(20):
        time.sleep(0.1)
        try:
            with socket.create_connection((HOST, PORT), timeout=0.25):
                return True
        except OSError:
            continue
    return False


def bridge_connected() -> bool:
    try:
        with urlopen(f"http://{HOST}:{PORT}/health", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def launch_dedicated_chrome(urls: list[str]) -> subprocess.Popen:
    chrome = chrome_or_error()
    profile = dedicated_profile_path()
    if not is_dedicated_profile_safe(profile):
        raise RuntimeError("Refusing to use a regular Chrome profile directory.")
    profile.mkdir(parents=True, exist_ok=True)
    args = [str(chrome), f"--user-data-dir={profile}", "--profile-directory=Default", "--no-first-run", "--no-default-browser-check", "--new-window"]
    if not EXTENSION_DIR.is_dir():
        raise RuntimeError("The local Chrome extension directory is missing. Reinstall the repository files.")
    args.extend(urls)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def open_extension_directory() -> None:
    if platform.system().lower() == "windows":
        os.startfile(str(EXTENSION_DIR))  # type: ignore[attr-defined]
    elif platform.system().lower() == "darwin":
        subprocess.Popen(["open", str(EXTENSION_DIR)])
    else:
        subprocess.Popen(["xdg-open", str(EXTENSION_DIR)])


def browser_setup() -> dict[str, str]:
    chrome = chrome_or_error()
    if not ensure_bridge():
        raise RuntimeError("Could not start the local bridge on 127.0.0.1. Check whether port 8765 is in use.")
    profile = dedicated_profile_path()
    profile.mkdir(parents=True, exist_ok=True)
    launch_dedicated_chrome(["chrome://extensions"])
    open_extension_directory()
    return {"chrome": str(chrome), "profile": str(profile), "extension": str(EXTENSION_DIR)}


def extension_handshake() -> dict[str, Any] | None:
    try:
        return json.loads(HANDSHAKE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def browser_verify(*, launch: bool = True) -> dict[str, Any]:
    chrome = find_chrome()
    profile = dedicated_profile_path()
    safe = is_dedicated_profile_safe(profile)
    bridge = ensure_bridge() and bridge_connected()
    launched = False
    if chrome and safe and launch:
        # A harmless public Luma landing page lets an installed content script
        # report its handshake without opening an application or submitting data.
        launch_dedicated_chrome(["https://luma.com/"])
        launched = True
        for _ in range(15):
            time.sleep(0.2)
            if extension_handshake():
                break
    handshake = extension_handshake()
    return {
        "chrome": str(chrome) if chrome else "",
        "profile": str(profile), "profile_exists": profile.is_dir(), "profile_safe": safe,
        "bridge": bridge, "launched": launched, "extension": handshake,
        "luma_session": (handshake or {}).get("luma_session", "unknown"),
    }
