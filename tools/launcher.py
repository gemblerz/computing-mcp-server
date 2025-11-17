"""Cross-platform application launcher helpers exposed to MCP tools."""

from __future__ import annotations

import functools
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Linux desktop entry parsing helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesktopEntry:
    """Minimal representation of a .desktop entry used for launching."""

    id: str
    name: str
    exec: List[str]
    file: Path


_DESKTOP_FIELD_CODE_RE = re.compile(r"%[fFuUdDnNickvmK]")

LINUX_DESKTOP_DIRS: List[Path] = [
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path.home() / ".local/share/applications",
    Path("/var/lib/flatpak/exports/share/applications"),
    Path.home() / ".local/share/flatpak/exports/share/applications",
    Path("/var/lib/snapd/desktop/applications"),
]


def _strip_exec_field_codes(exec_str: str) -> str:
    cleaned = _DESKTOP_FIELD_CODE_RE.sub("", exec_str)
    return re.sub(r"\s+", " ", cleaned).strip()


def _split_exec(exec_str: str) -> List[str]:
    cleaned = _strip_exec_field_codes(exec_str)
    if not cleaned:
        return []
    try:
        return shlex.split(cleaned)
    except ValueError:
        return cleaned.split()


def _iter_desktop_files() -> Iterable[Path]:
    seen: set[Path] = set()
    for base in LINUX_DESKTOP_DIRS:
        if not base.exists():
            continue
        for entry in base.glob("*.desktop"):
            if entry not in seen:
                seen.add(entry)
                yield entry


def _parse_desktop_file(path: Path) -> Optional[DesktopEntry]:
    name: Optional[str] = None
    exec_val: Optional[str] = None
    no_display = False

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith(("#", ";")):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key_low = key.strip().lower()
                if key_low == "name" and not name:
                    name = value.strip()
                elif key_low.startswith("name[") and not name:
                    name = value.strip()
                elif key_low == "exec" and not exec_val:
                    exec_val = value.strip()
                elif key_low == "nodisplay":
                    no_display = value.strip().lower() in {"1", "true", "yes"}
    except Exception:
        return None

    if no_display or not name or not exec_val:
        return None

    argv = _split_exec(exec_val)
    if not argv:
        return None
    return DesktopEntry(id=path.stem, name=name, exec=argv, file=path)


@functools.lru_cache(maxsize=1)
def _gather_linux_apps() -> Dict[str, DesktopEntry]:
    apps: Dict[str, DesktopEntry] = {}
    for desktop in _iter_desktop_files():
        entry = _parse_desktop_file(desktop)
        if not entry:
            continue
        apps.setdefault(entry.name.lower(), entry)
        apps.setdefault(entry.id.lower(), entry)
    return apps


def _resolve_linux_application(app_name: str) -> Optional[DesktopEntry]:
    normalized = app_name.strip().lower()
    if not normalized:
        return None
    apps = _gather_linux_apps()
    if normalized in apps:
        return apps[normalized]
    for key, entry in apps.items():
        if normalized in key:
            return entry
    return None


def _launch_linux(app_name: str, delay_seconds: int) -> bool:
    entry = _resolve_linux_application(app_name)
    if entry:
        argv = list(entry.exec)
        pretty = entry.name
    else:
        which = shutil.which(app_name)
        argv = [which] if which else [app_name]
        pretty = which or app_name

    def _do_launch() -> None:
        try:
            subprocess.Popen(argv)
            print(f"✓ Launched {pretty}")
        except Exception as exc:
            print(f"✗ Error launching {app_name}: {exc}")

    if delay_seconds > 0:
        def _delayed() -> None:
            time.sleep(delay_seconds)
            _do_launch()

        threading.Thread(target=_delayed, daemon=True).start()
        print(f"Scheduled {pretty} to launch in {delay_seconds} seconds...")
        return True

    _do_launch()
    return True


# ---------------------------------------------------------------------------
# Windows helpers (Start Menu + Microsoft Store)
# ---------------------------------------------------------------------------


def get_start_menu_paths() -> List[Path]:
    """Get all Windows Start Menu directories."""
    paths = []
    user_start = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    if user_start.exists():
        paths.append(user_start)

    programdata = os.environ.get("PROGRAMDATA", "C:\\ProgramData")
    all_users_start = Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    if all_users_start.exists():
        paths.append(all_users_start)
    return paths


def get_microsoft_store_apps() -> List[tuple]:
    store_apps: List[tuple] = []
    try:
        ps_command = "Get-AppxPackage | Select-Object Name, PackageFamilyName"
        result = subprocess.run(
            ["powershell", "-Command", ps_command],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            for line in lines[3:]:
                line = line.strip()
                if line:
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        name = parts[0]
                        family_name = parts[1]
                        store_apps.append((name, family_name))
    except Exception:
        pass
    return store_apps


def search_start_menu(app_name: str, verbose: bool = False) -> List[str]:
    app_name_lower = app_name.lower()
    shortcuts: List[str] = []
    start_menu_paths = get_start_menu_paths()

    if verbose:
        print(f"Searching in {len(start_menu_paths)} Start Menu locations...")
        for path in start_menu_paths:
            print(f"  - {path}")

    for start_menu_path in start_menu_paths:
        for root, _dirs, files in os.walk(start_menu_path):
            for file in files:
                if file.lower().endswith(".lnk") and app_name_lower in file.lower():
                    shortcuts.append(os.path.join(root, file))
                    if verbose:
                        print(f"  ✓ Match: {file}")
    return shortcuts


def search_store_apps(app_name: str, verbose: bool = False) -> Optional[str]:
    app_name_lower = app_name.lower()
    if verbose:
        print("Searching Microsoft Store apps...")
    store_apps = get_microsoft_store_apps()
    for name, package_family in store_apps:
        if app_name_lower in name.lower():
            if verbose:
                print(f"  ✓ Found Store app: {name}")
            return f"shell:AppsFolder\\{package_family}!App"
    return None


def _launch_windows(app_name: str, delay_seconds: int) -> bool:
    shortcuts = search_start_menu(app_name, verbose=False)

    def _launch_shortcut(path: str) -> None:
        if delay_seconds > 0:
            print(f"Waiting {delay_seconds} seconds before launching {Path(path).stem}...")
            time.sleep(delay_seconds)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
            print(f"✓ Launched {Path(path).stem}")
        except Exception as exc:
            print(f"✗ Error launching {path}: {exc}")

    if shortcuts:
        chosen = shortcuts[0]
        if len(shortcuts) == 1:
            print(f"Found: {Path(chosen).stem}")
        else:
            print(f"Found {len(shortcuts)} matches, using {Path(chosen).stem}")
        if delay_seconds > 0:
            threading.Thread(target=_launch_shortcut, args=(chosen,), daemon=True).start()
            print(f"Scheduled {Path(chosen).stem} to launch in {delay_seconds} seconds...")
        else:
            _launch_shortcut(chosen)
        return True

    print("Not found in Start Menu, checking Microsoft Store apps...")
    store_app = search_store_apps(app_name, verbose=True)
    if store_app:
        def _launch_store() -> None:
            if delay_seconds > 0:
                print(f"Waiting {delay_seconds} seconds before launching...")
                time.sleep(delay_seconds)
            try:
                subprocess.Popen(["explorer.exe", store_app])
                print(f"✓ Launched {app_name}")
            except Exception as exc:
                print(f"✗ Error: {exc}")

        if delay_seconds > 0:
            threading.Thread(target=_launch_store, daemon=True).start()
            print(f"Scheduled to launch in {delay_seconds} seconds...")
        else:
            _launch_store()
        return True

    print("Trying Windows built-in command as fallback...")

    def _fallback() -> None:
        if delay_seconds > 0:
            print(f"Waiting {delay_seconds} seconds before launching {app_name}...")
            time.sleep(delay_seconds)
        try:
            subprocess.Popen([
                "cmd",
                "/c",
                "start",
                "",
                app_name,
            ], shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"✓ Launched {app_name}")
        except Exception as exc:
            print(f"✗ Error launching {app_name}: {exc}")

    if delay_seconds > 0:
        threading.Thread(target=_fallback, daemon=True).start()
        print(f"Scheduled {app_name} to launch in {delay_seconds} seconds...")
    else:
        _fallback()
    return True


# ---------------------------------------------------------------------------
# macOS helpers
# ---------------------------------------------------------------------------


MAC_APP_ALIASES: Dict[str, str] = {
    "garageband": "/Applications/GarageBand.app",
    "garage band": "/Applications/GarageBand.app",
    "music": "/System/Applications/Music.app",
    "terminal": "/System/Applications/Utilities/Terminal.app",
    "safari": "/Applications/Safari.app",
    "notes": "/System/Applications/Notes.app",
}

MAC_SEARCH_DIRECTORIES = [
    Path("/Applications"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path.home() / "Applications",
]


@functools.lru_cache(maxsize=1)
def _gather_mac_apps() -> Dict[str, Path]:
    apps: Dict[str, Path] = {}
    for alias, target in MAC_APP_ALIASES.items():
        location = Path(target)
        if location.exists():
            apps[alias] = location
    for directory in MAC_SEARCH_DIRECTORIES:
        if not directory.exists():
            continue
        for bundle in directory.glob("**/*.app"):
            apps.setdefault(bundle.stem.lower(), bundle)
    return apps


def _resolve_mac_application(app_name: str) -> Optional[Path]:
    candidate = Path(app_name).expanduser()
    if candidate.suffix.lower() == ".app" and candidate.exists():
        return candidate
    normalized = app_name.strip().lower()
    if not normalized:
        return None
    aliases = _gather_mac_apps()
    if normalized in aliases:
        return aliases[normalized]
    for key, bundle in aliases.items():
        if normalized in key:
            return bundle
    return None


def _launch_macos(app_name: str, delay_seconds: int) -> bool:
    resolved = _resolve_mac_application(app_name)

    def _do_launch() -> None:
        try:
            if resolved:
                subprocess.Popen(["open", str(resolved)])
                pretty = resolved.stem
            else:
                subprocess.Popen(["open", "-a", app_name])
                pretty = app_name
            print(f"✓ Launched {pretty}")
        except Exception as exc:
            print(f"✗ {exc}")

    if delay_seconds > 0:
        def _delayed() -> None:
            time.sleep(delay_seconds)
            _do_launch()

        threading.Thread(target=_delayed, daemon=True).start()
        pretty = resolved.stem if resolved else app_name
        print(f"Scheduled {pretty} to launch in {delay_seconds} seconds...")
        return True

    _do_launch()
    return True


# ---------------------------------------------------------------------------
# Generic fallback
# ---------------------------------------------------------------------------


def _launch_generic(app_name: str, delay_seconds: int) -> bool:
    def _do_launch() -> None:
        try:
            subprocess.Popen([app_name])
            print(f"✓ Launched {app_name}")
        except Exception as exc:
            print(f"✗ Error launching {app_name}: {exc}")

    if delay_seconds > 0:
        def _delayed() -> None:
            time.sleep(delay_seconds)
            _do_launch()

        threading.Thread(target=_delayed, daemon=True).start()
        print(f"Scheduled {app_name} to launch in {delay_seconds} seconds...")
        return True

    _do_launch()
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def launch(app_name: str, delay_seconds: int = 0) -> bool:
    if sys.platform == "darwin":
        return _launch_macos(app_name, delay_seconds)
    if os.name == "nt":
        return _launch_windows(app_name, delay_seconds)
    if sys.platform.startswith("linux"):
        return _launch_linux(app_name, delay_seconds)
    return _launch_generic(app_name, delay_seconds)


def launch_now(app_name: str) -> bool:
    return launch(app_name, 0)


def launch_in(app_name: str, seconds: int) -> bool:
    return launch(app_name, seconds)


def search(app_name: str) -> List[str]:
    results: List[str] = []
    shortcuts = search_start_menu(app_name, verbose=False)
    for shortcut in shortcuts:
        title = Path(shortcut).stem
        if title not in results:
            results.append(title)

    store_app = search_store_apps(app_name, verbose=False)
    if store_app:
        results.append(app_name.title())

    if sys.platform == "darwin":
        normalized = app_name.strip().lower()
        resolved = _resolve_mac_application(app_name)
        if resolved and resolved.stem not in results:
            results.append(resolved.stem)
        else:
            for key, bundle in _gather_mac_apps().items():
                if normalized and normalized in key:
                    name = bundle.stem
                    if name not in results:
                        results.append(name)

    if sys.platform.startswith("linux"):
        normalized = app_name.strip().lower()
        apps = _gather_linux_apps()
        seen = {name.lower() for name in results}
        for key, entry in apps.items():
            if normalized and normalized in key:
                friendly = entry.name
                if friendly.lower() not in seen:
                    results.append(friendly)
                    seen.add(friendly.lower())

    return results


def list_installed_apps(search_term: str = "") -> List[str]:
    shortcuts: List[str] = []
    search_lower = search_term.lower()
    for start_menu_path in get_start_menu_paths():
        for _root, _dirs, files in os.walk(start_menu_path):
            for file in files:
                if file.lower().endswith(".lnk"):
                    app_name = Path(file).stem
                    if not search_term or search_lower in app_name.lower():
                        shortcuts.append(app_name)
    return sorted(set(shortcuts))


def list_apps(filter_term: str = "") -> List[str]:
    if sys.platform == "darwin":
        apps = sorted({path.stem for path in _gather_mac_apps().values()})
        if filter_term:
            term = filter_term.lower()
            apps = [name for name in apps if term in name.lower()]
        return apps
    if os.name == "nt":
        return list_installed_apps(filter_term)
    if sys.platform.startswith("linux"):
        entries = _gather_linux_apps()
        names = sorted({entry.name for entry in entries.values()})
        if filter_term:
            term = filter_term.lower()
            names = [name for name in names if term in name.lower()]
        return names
    return []


if __name__ == "__main__":
    print("Launcher module loaded. Try running via MCP tools instead of directly.")
