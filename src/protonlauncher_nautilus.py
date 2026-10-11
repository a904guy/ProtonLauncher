"""
Proton Launcher for GNOME Files (Nautilus).

Adds "Run with Proton" to the right-click menu of Windows programs, plus a
"Proton Launcher" submenu with per-program tools. All real work happens in
protonlauncher.py, which this extension imports from the install directory
and runs as a separate process so Files never blocks on Wine.

Works with the Nautilus 4.x (GNOME 43+) and 3.0 python bindings.
"""

import importlib.util
import os
import shutil
import subprocess
import time
from urllib.parse import unquote, urlparse

import gi

for _ver in ("4.1", "4.0", "3.0"):
    try:
        gi.require_version("Nautilus", _ver)
        break
    except ValueError:
        continue

from gi.repository import GObject, Nautilus  # noqa: E402

CORE = os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
    "protonlauncher", "protonlauncher.py")
VERSION_CACHE_SECONDS = 30


def _load_core():
    try:
        spec = importlib.util.spec_from_file_location("protonlauncher", CORE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _local_path(file_info):
    location = file_info.get_location()
    path = location.get_path() if location is not None else None
    if path:
        return path
    uri = file_info.get_uri() or ""
    return unquote(urlparse(uri).path) if uri.startswith("file://") else None


class ProtonLauncherExtension(GObject.GObject, Nautilus.MenuProvider):
    def __init__(self):
        super().__init__()
        self.core = _load_core()
        self._protons = []
        self._protons_at = 0.0

    # -- helpers ------------------------------------------------------------

    def _protons_cached(self):
        if time.monotonic() - self._protons_at > VERSION_CACHE_SECONDS:
            try:
                self._protons = self.core.find_protons()
            except Exception:
                self._protons = []
            self._protons_at = time.monotonic()
        return self._protons

    def _spawn(self, *args):
        subprocess.Popen(
            [self.core.LAUNCHER, "--gui"] + list(args),
            cwd=os.path.expanduser("~"),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)

    def _item(self, name, label, tip, *args):
        item = Nautilus.MenuItem(name="ProtonLauncher::" + name, label=label, tip=tip)
        item.connect("activate", lambda *_: self._spawn(*args))
        return item

    def _supported_paths(self, files):
        paths = []
        for f in files:
            if f.is_directory():
                return []
            path = _local_path(f)
            if not path or os.path.splitext(path)[1].lower() not in self.core.SUPPORTED_EXTENSIONS:
                return []
            paths.append(path)
        return paths

    # -- Nautilus API ---------------------------------------------------------

    def get_file_items(self, *args):
        # Nautilus 4.x passes (files); 3.0 passes (window, files).
        files = args[-1]
        if self.core is None or not files:
            return []
        paths = self._supported_paths(files)
        if not paths:
            return []

        run = Nautilus.MenuItem(name="ProtonLauncher::run", label="Run with Proton",
                                tip="Run this Windows program with Steam's Proton")
        run.connect("activate", lambda *_: [self._spawn("run", p) for p in paths])
        if len(paths) > 1:
            return [run]

        path = paths[0]
        prefix = self.core.prefix_for(path)
        has_prefix = self.core.prefix_exists(prefix)
        current = self.core.load_meta(prefix).get("proton")

        submenu = Nautilus.Menu()
        protons = self._protons_cached()
        if protons:
            versions = Nautilus.Menu()
            for i, proton in enumerate(protons):
                label = proton.name + ("  ✓" if proton.name == current else "")
                versions.append_item(self._item(
                    "version-{}".format(i), label,
                    "Run with {} and keep using it for this program".format(proton.name),
                    "run", "--proton", proton.name, path))
            pick = Nautilus.MenuItem(name="ProtonLauncher::versions", label="Run With Version",
                                     tip="Choose which Proton build runs this program")
            pick.set_submenu(versions)
            submenu.append_item(pick)
        submenu.append_item(self._item("shortcut", "Add to Applications Menu",
                                       "Put this program in your app grid", "shortcut", path))
        if has_prefix:
            submenu.append_item(self._item("open", "Browse C: Drive",
                                           "Open this program's Windows C: drive", "open-prefix", path))
        submenu.append_item(self._item("winecfg", "Wine Settings",
                                       "Windows version, drives, audio and graphics settings", "winecfg", path))
        if shutil.which("winetricks"):
            submenu.append_item(self._item("winetricks", "Winetricks",
                                           "Install runtimes like .NET, Visual C++ and fonts", "winetricks", path))
        if os.path.isdir(prefix):
            submenu.append_item(self._item("delete", "Delete Prefix…",
                                           "Remove this program's Windows prefix and start fresh",
                                           "delete-prefix", path))

        tools = Nautilus.MenuItem(name="ProtonLauncher::tools", label="Proton Launcher",
                                  tip="More Proton options for this program")
        tools.set_submenu(submenu)
        return [run, tools]

    def get_background_items(self, *args):
        return []
