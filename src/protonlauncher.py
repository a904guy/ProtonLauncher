#!/usr/bin/env python3
"""
Proton Launcher - run Windows programs with the Proton builds Steam installs.

Steam keeps its Proton tools up to date, so launching through them means the
compatibility layer updates itself without any extra work. Every program gets
its own Wine prefix under ~/.local/share/protonlauncher/prefixes so one app's
runtimes and registry tweaks never break another.

This single file is the whole engine: the command line tool, the code the
Nautilus extension imports, and the handler behind "Open With".
Python 3.8+ and the standard library only.
"""

import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time

__version__ = "1.0.0"

APP_ID = "protonlauncher"
APP_NAME = "Proton Launcher"
SUPPORTED_EXTENSIONS = (".exe", ".msi", ".bat", ".cmd")
DEFAULT_PROTON = "Proton - Experimental"
# Proton runs everything as this Windows user.
PROTON_USER = "steamuser"


def _xdg(var, fallback):
    return os.environ.get(var) or os.path.expanduser(fallback)


DATA_DIR = os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), APP_ID)
PREFIX_ROOT = os.path.join(DATA_DIR, "prefixes")
ICON_DIR = os.path.join(DATA_DIR, "icons")
APPS_DIR = os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "applications")
CONFIG_FILE = os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), APP_ID, "config.ini")
LOG_DIR = os.path.join(_xdg("XDG_CACHE_HOME", "~/.cache"), APP_ID)
LAUNCHER = os.path.realpath(__file__)
# Installed next to the launcher; an absolute path sidesteps stale icon-theme caches.
_ICON_FILE = os.path.join(os.path.dirname(LAUNCHER), "protonlauncher.svg")
APP_ICON = _ICON_FILE if os.path.exists(_ICON_FILE) else APP_ID

STEAM_ROOT_CANDIDATES = (
    "~/.local/share/Steam",
    "~/.steam/steam",
    "~/.steam/root",
    "~/.var/app/com.valvesoftware.Steam/data/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
    "~/snap/steam/common/.local/share/Steam",
)
SYSTEM_COMPAT_DIRS = (
    "/usr/share/steam/compatibilitytools.d",
    "/usr/local/share/steam/compatibilitytools.d",
)

# Exit codes that mean "the user backed out", not "it broke".
QUIET_EXIT_CODES = {0, 1602}  # 1602 = MSI install cancelled by user

INSTALLER_HINT = re.compile(r"setup|install", re.IGNORECASE)
NOT_A_LAUNCHER = re.compile(r"unins|uninst|remove|repair|readme|help|manual|website", re.IGNORECASE)


class LauncherError(Exception):
    """A problem worth showing to the user as-is."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

CONFIG_TEMPLATE = """\
# Proton Launcher settings. Lines starting with # are ignored.

[launcher]
# Proton build used for programs that have not picked one yet.
# Run `proton-launch list` to see the names you can use here.
# proton = Proton - Experimental

# Set to yes to write Wine debug logs (large!) to ~/.cache/protonlauncher.
# verbose_logs = no

[env]
# Extra environment variables for every program, e.g.
# DXVK_HUD = fps
# PROTON_USE_WINED3D = 1
"""


def load_config():
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # keep env var names case-sensitive
    try:
        parser.read(CONFIG_FILE, encoding="utf-8")
    except configparser.Error as exc:
        raise LauncherError("Could not read {}: {}".format(CONFIG_FILE, exc)) from exc
    section = parser["launcher"] if parser.has_section("launcher") else {}
    return {
        "proton": (section.get("proton") or "").strip() or None,
        "verbose_logs": str(section.get("verbose_logs", "no")).strip().lower() in ("1", "yes", "true", "on"),
        "env": dict(parser["env"]) if parser.has_section("env") else {},
    }


def ensure_config_file():
    if not os.path.exists(CONFIG_FILE):
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            fh.write(CONFIG_TEMPLATE)
    return CONFIG_FILE


# --------------------------------------------------------------------------
# Finding Steam and Proton
# --------------------------------------------------------------------------

_VDF_PATH = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"')


def steam_roots():
    """Steam installs on this machine (native, Flatpak, Snap), deduplicated."""
    roots = []
    for candidate in STEAM_ROOT_CANDIDATES:
        path = os.path.expanduser(candidate)
        if not (os.path.isdir(os.path.join(path, "steamapps"))
                or os.path.isdir(os.path.join(path, "compatibilitytools.d"))):
            continue
        real = os.path.realpath(path)
        if real not in roots:
            roots.append(real)
    return roots


def parse_library_paths(vdf_text):
    """Library folder paths listed in a Steam libraryfolders.vdf."""
    return [re.sub(r"\\(.)", r"\1", m) for m in _VDF_PATH.findall(vdf_text)]


def library_dirs(roots):
    libs = []
    for root in roots:
        candidates = [root]
        try:
            with open(os.path.join(root, "steamapps", "libraryfolders.vdf"), encoding="utf-8", errors="replace") as fh:
                candidates += parse_library_paths(fh.read())
        except OSError:
            pass
        for lib in candidates:
            if os.path.isdir(os.path.join(lib, "steamapps")):
                real = os.path.realpath(lib)
                if real not in libs:
                    libs.append(real)
    return libs


def is_proton_dir(path):
    if not os.path.isfile(os.path.join(path, "proton")):
        return False
    # Newer builds keep Wine in files/, older ones (3.x-5.x) in dist/ or a
    # tarball unpacked on first run. Filters out things like the BattlEye runtime.
    return any(os.path.exists(os.path.join(path, p))
               for p in ("files/bin/wine", "dist/bin/wine", "proton_dist.tar.gz"))


def _natural_key(text):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


def _rank(name):
    if name == DEFAULT_PROTON:
        return 0
    if re.match(r"Proton \d", name):
        return 1
    if name.startswith("Proton"):
        return 2
    return 3


class Proton:
    def __init__(self, name, path):
        self.name = name
        self.path = path

    @property
    def script(self):
        return os.path.join(self.path, "proton")

    def bin(self, tool):
        for sub in ("files", "dist"):
            candidate = os.path.join(self.path, sub, "bin", tool)
            if os.path.exists(candidate):
                return candidate
        return None

    def __repr__(self):
        return "Proton({!r})".format(self.name)


def find_protons():
    """All usable Proton builds, best default first."""
    roots = steam_roots()
    search = [os.path.join(lib, "steamapps", "common") for lib in library_dirs(roots)]
    search += [os.path.join(root, "compatibilitytools.d") for root in roots]
    search += list(SYSTEM_COMPAT_DIRS)
    found = {}
    for base in search:
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in entries:
            path = os.path.join(base, entry)
            if entry not in found and is_proton_dir(path):
                found[entry] = os.path.realpath(path)
    names = sorted(found, key=_natural_key, reverse=True)
    names.sort(key=_rank)  # stable: keeps newest-first inside each group
    return [Proton(n, found[n]) for n in names]


def default_proton_name(protons, config=None):
    config = config if config is not None else load_config()
    names = [p.name for p in protons]
    if config["proton"] in names:
        return config["proton"]
    return names[0] if names else None


def choose_proton(requested=None, config=None):
    """Return (Proton, note). note explains a fallback, else None."""
    protons = find_protons()
    if not protons:
        raise LauncherError(
            "No Proton found. In Steam, open Library, search for "
            "\"Proton Experimental\" (it is listed under Tools) and install it.")
    by_name = {p.name: p for p in protons}
    if requested and requested in by_name:
        return by_name[requested], None
    fallback = by_name[default_proton_name(protons, config)]
    note = None
    if requested:
        note = "{} is no longer installed, using {} instead.".format(requested, fallback.name)
    return fallback, note


def steam_client_dir():
    roots = steam_roots()
    return roots[0] if roots else os.path.expanduser("~/.local/share/Steam")


# --------------------------------------------------------------------------
# Prefixes
# --------------------------------------------------------------------------

def _slug(text, limit=40):
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return text[:limit] or "app"


def prefix_for(target):
    """The prefix directory a file belongs to.

    Files that live inside an existing prefix (a program an installer put
    under drive_c) reuse that prefix. Anything else gets one keyed by its
    full path, so two installers sitting in ~/Downloads stay separate.
    """
    real = os.path.realpath(target)
    root = os.path.realpath(PREFIX_ROOT) + os.sep
    if real.startswith(root):
        return os.path.join(PREFIX_ROOT, real[len(root):].split(os.sep, 1)[0])
    stem = os.path.splitext(os.path.basename(real))[0]
    digest = hashlib.sha1(real.encode("utf-8", "surrogateescape")).hexdigest()[:8]
    return os.path.join(PREFIX_ROOT, "{}-{}".format(_slug(stem), digest))


def prefix_exists(prefix):
    return os.path.isdir(os.path.join(prefix, "pfx", "drive_c"))


def drive_c(prefix):
    return os.path.join(prefix, "pfx", "drive_c")


def system32(prefix):
    return os.path.join(drive_c(prefix), "windows", "system32")


def _meta_path(prefix):
    return os.path.join(prefix, "protonlauncher.json")


def load_meta(prefix):
    try:
        with open(_meta_path(prefix), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_meta(prefix, meta):
    os.makedirs(prefix, exist_ok=True)
    tmp = _meta_path(prefix) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
    os.replace(tmp, _meta_path(prefix))


def list_prefixes():
    try:
        names = sorted(os.listdir(PREFIX_ROOT))
    except OSError:
        return []
    return [os.path.join(PREFIX_ROOT, n) for n in names if os.path.isdir(os.path.join(PREFIX_ROOT, n))]


def windows_path(unix_path):
    """Proton maps Z: to /, so any absolute path has a Windows spelling."""
    return "Z:" + os.path.abspath(unix_path).replace("/", "\\")


def unix_path(prefix, win_path):
    """Resolve a Windows path inside a prefix, matching case like Windows does."""
    match = re.match(r"^([A-Za-z]):[\\/]*(.*)$", win_path or "")
    if not match:
        return None
    current = os.path.join(prefix, "pfx", "dosdevices", match.group(1).lower() + ":")
    for part in re.split(r"[\\/]+", match.group(2)):
        if not part:
            continue
        exact = os.path.join(current, part)
        if os.path.exists(exact):
            current = exact
            continue
        try:
            hits = [e for e in os.listdir(current) if e.lower() == part.lower()]
        except OSError:
            return None
        if not hits:
            return None
        current = os.path.join(current, hits[0])
    return os.path.realpath(current)


# --------------------------------------------------------------------------
# Windows file formats: shortcuts (.lnk) and icons inside .exe files
# --------------------------------------------------------------------------

def parse_lnk(data):
    """Pull target, arguments and working dir out of a Windows .lnk file.

    Implements just enough of MS-SHLLINK for what installers write.
    """
    if len(data) < 0x4C or struct.unpack_from("<I", data, 0)[0] != 0x4C:
        return None
    flags = struct.unpack_from("<I", data, 0x14)[0]
    unicode = bool(flags & 0x80)
    pos = 0x4C
    if flags & 0x01:  # HasLinkTargetIDList
        pos += 2 + struct.unpack_from("<H", data, pos)[0]
    target = None
    if flags & 0x02:  # HasLinkInfo
        info = pos
        size, header, info_flags = struct.unpack_from("<III", data, info)
        base_off, _net_off, suffix_off = struct.unpack_from("<III", data, info + 16)
        if info_flags & 0x01:

            def cstr(off, wide):
                if wide:
                    end = off
                    while data[end:end + 2] != b"\0\0":
                        end += 2
                    return data[off:end].decode("utf-16-le", "replace")
                return data[off:data.index(b"\0", off)].decode("cp1252", "replace")

            if header >= 0x24:
                base_u, suffix_u = struct.unpack_from("<II", data, info + 28)
                target = cstr(info + base_u, True) + cstr(info + suffix_u, True)
            else:
                target = cstr(info + base_off, False) + cstr(info + suffix_off, False)
        pos += size
    strings = {}
    for bit, key in ((0x04, "name"), (0x08, "relative_path"), (0x10, "working_dir"),
                     (0x20, "arguments"), (0x40, "icon_location")):
        if flags & bit:
            count = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            width = 2 if unicode else 1
            raw = data[pos:pos + count * width]
            # Wine counts the terminating NUL; Windows does not.
            strings[key] = raw.decode("utf-16-le" if unicode else "cp1252", "replace").rstrip("\0")
            pos += count * width
    strings["target"] = target
    return strings


def _pe_resources(data):
    """Map (type, id) -> bytes for resources in a PE file."""
    if data[:2] != b"MZ":
        return {}
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        return {}
    sections, opt_size = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dirs = opt + (96 if magic == 0x10B else 112)
    res_rva = struct.unpack_from("<I", data, dirs + 2 * 8)[0]
    if not res_rva:
        return {}
    table = opt + opt_size
    spans = []
    for i in range(sections):
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, table + i * 40 + 8)
        spans.append((vaddr, max(vsize, rawsize), rawptr))

    def offset(rva):
        for vaddr, size, rawptr in spans:
            if vaddr <= rva < vaddr + size:
                return rva - vaddr + rawptr
        raise ValueError("RVA outside sections")

    base = offset(res_rva)

    def entries(off):
        named, ids = struct.unpack_from("<HH", data, base + off + 12)
        for i in range(named + ids):
            ident, child = struct.unpack_from("<II", data, base + off + 16 + i * 8)
            yield ident, child

    out = {}
    for rtype, type_off in entries(0):
        if rtype not in (3, 14) or not type_off & 0x80000000:
            continue
        for rid, id_off in entries(type_off & 0x7FFFFFFF):
            for _lang, leaf in entries(id_off & 0x7FFFFFFF) if id_off & 0x80000000 else [(0, id_off)]:
                rva, size = struct.unpack_from("<II", data, base + (leaf & 0x7FFFFFFF))
                start = offset(rva)
                out.setdefault((rtype, rid), data[start:start + size])
    return out


def extract_icon(exe_path, out_base):
    """Save the program's main icon next to out_base (.png or .ico). Returns the path or None."""
    try:
        with open(exe_path, "rb") as fh:
            data = fh.read()
        res = _pe_resources(data)
        groups = sorted(k for k in res if k[0] == 14)
        if not groups:
            return None
        group = res[groups[0]]
        count = struct.unpack_from("<H", group, 4)[0]
        images = []
        for i in range(count):
            entry = group[6 + i * 14:6 + (i + 1) * 14]
            width, height = entry[0] or 256, entry[1] or 256
            icon_id = struct.unpack_from("<H", entry, 12)[0]
            blob = res.get((3, icon_id))
            if blob:
                images.append((width * height, struct.unpack_from("<H", entry, 6)[0], entry[:12], blob))
        if not images:
            return None
        images.sort(key=lambda im: (im[0], im[1]), reverse=True)
        os.makedirs(os.path.dirname(out_base), exist_ok=True)
        if images[0][3][:8] == b"\x89PNG\r\n\x1a\n":
            path = out_base + ".png"
            with open(path, "wb") as fh:
                fh.write(images[0][3])
            return path
        path = out_base + ".ico"
        header = struct.pack("<HHH", 0, 1, len(images))
        offset = 6 + 16 * len(images)
        directory, blobs = b"", b""
        for _area, _bits, fields, blob in images:
            directory += fields[:8] + struct.pack("<II", len(blob), offset + len(blobs))
            blobs += blob
        with open(path, "wb") as fh:
            fh.write(header + directory + blobs)
        return path
    except (OSError, ValueError, struct.error, IndexError):
        return None


# --------------------------------------------------------------------------
# Desktop integration
# --------------------------------------------------------------------------

def desktop_exec_arg(arg):
    """Quote one argument for a .desktop Exec= line (spec quoting + key-file escaping)."""
    quoted = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    return quoted.replace("\\", "\\\\").replace("%", "%%")


def _desktop_value(text):
    return text.replace("\\", "\\\\").replace("\n", " ").replace("\r", " ")


def create_shortcut(target, name=None, args=(), prefix=None):
    """Add a program to the desktop's Applications menu. Returns the .desktop path."""
    target = os.path.abspath(target)
    prefix = prefix or prefix_for(target)
    name = (name or os.path.splitext(os.path.basename(target))[0]).strip() or "Windows Program"
    prefix_id = os.path.basename(prefix)
    file_id = "{}-{}-{}".format(APP_ID, prefix_id, _slug(name, 30).lower())
    icon = extract_icon(target, os.path.join(ICON_DIR, file_id)) or APP_ICON
    exec_line = " ".join(desktop_exec_arg(a) for a in [LAUNCHER, "run", target] + (["--"] + list(args) if args else []))
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=" + _desktop_value(name),
        "Comment=Windows program, runs with Proton",
        "Exec=" + exec_line,
        "Path=" + _desktop_value(os.path.dirname(target)),
        "Icon=" + _desktop_value(icon),
        "Terminal=false",
        "Categories=Wine;",
        # Wine names windows after the .exe, which lets the dock group them.
        "StartupWMClass=" + _desktop_value(os.path.basename(target).lower()),
        "X-ProtonLauncher-Prefix=" + prefix_id,
        "",
    ]
    os.makedirs(APPS_DIR, exist_ok=True)
    path = os.path.join(APPS_DIR, file_id + ".desktop")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.chmod(path, 0o755)
    if shutil.which("update-desktop-database"):
        subprocess.call(["update-desktop-database", "-q", APPS_DIR],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


def shortcuts_for_prefix(prefix):
    marker = "X-ProtonLauncher-Prefix=" + os.path.basename(prefix)
    found = []
    try:
        entries = os.listdir(APPS_DIR)
    except OSError:
        return found
    for entry in entries:
        if entry.startswith(APP_ID + "-") and entry.endswith(".desktop"):
            path = os.path.join(APPS_DIR, entry)
            try:
                with open(path, encoding="utf-8") as fh:
                    if marker in fh.read().splitlines():
                        found.append(path)
            except OSError:
                pass
    return found


# --------------------------------------------------------------------------
# Talking to the user
# --------------------------------------------------------------------------

class UI:
    """Prints for terminals; with gui=True also uses desktop notifications and zenity."""

    def __init__(self, gui):
        self.gui = gui
        self.zenity = shutil.which("zenity") if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) else None

    def notify(self, title, body="", urgent=False):
        print("{}{}".format(title, ": " + body if body else ""), file=sys.stderr)
        if self.gui and shutil.which("notify-send"):
            cmd = ["notify-send", "-a", APP_NAME, "-i", APP_ICON]
            if urgent:
                cmd += ["-u", "critical"]
            subprocess.call(cmd + [title, body], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def error(self, message):
        self.notify(APP_NAME, message, urgent=True)

    def _zenity(self, args):
        proc = subprocess.run([self.zenity, "--title=" + APP_NAME] + args,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        return proc.returncode, proc.stdout.strip()

    def confirm(self, question):
        if self.gui and self.zenity:
            return self._zenity(["--question", "--no-wrap", "--text=" + question])[0] == 0
        if sys.stdin.isatty():
            return input(question + " [y/N] ").strip().lower() in ("y", "yes")
        return False

    def ask_text(self, question, default):
        if self.gui and self.zenity:
            code, out = self._zenity(["--entry", "--text=" + question, "--entry-text=" + default])
            return out if code == 0 else None
        return default

    def pick_many(self, text, rows):
        """rows: [(key, label, detail)] -> chosen keys. Without zenity, everything is chosen."""
        if not (self.gui and self.zenity):
            return [r[0] for r in rows]
        args = ["--list", "--checklist", "--width=640", "--height=360", "--text=" + text,
                "--column=Add", "--column=Key", "--column=Name", "--column=Program",
                "--hide-column=2", "--print-column=2", "--separator=\n"]
        for key, label, detail in rows:
            args += ["TRUE", key, label, detail]
        code, out = self._zenity(args)
        return [line for line in out.splitlines() if line] if code == 0 else []

    def pick_one(self, text, rows):
        """rows: [(key, label)] -> chosen key or None."""
        if not self.zenity:
            raise LauncherError("zenity is needed for this menu. Install it, or use the proton-launch command.")
        args = ["--list", "--width=420", "--height=380", "--text=" + text, "--column=Key", "--column=Action",
                "--hide-column=1", "--print-column=1", "--hide-header"]
        for key, label in rows:
            args += [key, label]
        code, out = self._zenity(args)
        return out.splitlines()[0] if code == 0 and out else None

    def pick_file(self):
        if not self.zenity:
            return None
        code, out = self._zenity(["--file-selection", "--file-filter=Windows programs | *.exe *.EXE *.msi *.MSI *.bat *.cmd"])
        return out if code == 0 and out else None


# --------------------------------------------------------------------------
# Running things
# --------------------------------------------------------------------------

def proton_env(prefix, config):
    env = dict(os.environ)
    env.update(config["env"])
    env["STEAM_COMPAT_DATA_PATH"] = prefix
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = steam_client_dir()
    if config["verbose_logs"]:
        env["PROTON_LOG"] = "1"
        env["PROTON_LOG_DIR"] = LOG_DIR
    return env


def build_command(proton, prefix, target, args=()):
    """The proton invocation for a file.

    Proton only waits for (and reports the exit code of) programs given by
    absolute path; bare names like `cmd` are fire-and-forget. So .msi and
    .bat go through the prefix's own msiexec.exe / cmd.exe by full path.
    """
    ext = os.path.splitext(target)[1].lower()
    if ext == ".msi":
        program = [os.path.join(system32(prefix), "msiexec.exe"), "/i", windows_path(target)]
    elif ext in (".bat", ".cmd"):
        program = [os.path.join(system32(prefix), "cmd.exe"), "/c", windows_path(target)]
    else:
        program = [target]
    return [proton.script, "run"] + program + list(args)


def _open_log(prefix):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, os.path.basename(prefix) + ".log")
    if os.path.exists(path):
        os.replace(path, path + ".1")
    return path, open(path, "w", encoding="utf-8", errors="replace")


def _proton_call(proton, prefix, argv, config, cwd, log_title):
    log_path, log = _open_log(prefix)
    with log:
        log.write("# {} {} - {}\n# proton: {} ({})\n# prefix: {}\n# command: {}\n\n".format(
            APP_NAME, __version__, time.strftime("%Y-%m-%d %H:%M:%S"), proton.name, proton.path,
            prefix, " ".join(argv)))
        log.write("# {}\n".format(log_title))
        log.flush()
        rc = subprocess.call(argv, cwd=cwd, env=proton_env(prefix, config),
                             stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        log.write("\n# exit code {}\n".format(rc))
    return rc, log_path


def wait_for_prefix_idle(proton, prefix, config, timeout=None):
    """Block until every program in the prefix has exited."""
    wineserver = proton.bin("wineserver")
    if not wineserver:
        return
    env = proton_env(prefix, config)
    env["WINEPREFIX"] = os.path.join(prefix, "pfx")
    try:
        subprocess.call([wineserver, "-w"], env=env, timeout=timeout,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass


def start_menu_links(prefix):
    roots = [
        "ProgramData/Microsoft/Windows/Start Menu",
        "users/{}/AppData/Roaming/Microsoft/Windows/Start Menu".format(PROTON_USER),
        "users/{}/Desktop".format(PROTON_USER),
        "users/Public/Desktop",
    ]
    links = set()
    for rel in roots:
        for dirpath, _dirs, files in os.walk(os.path.join(drive_c(prefix), rel)):
            links.update(os.path.join(dirpath, f) for f in files if f.lower().endswith(".lnk"))
    return links


def programs_from_links(prefix, links):
    """[(name, exe_path, args)] for shortcuts that point at real programs."""
    out, seen = [], set()
    for link in sorted(links):
        try:
            with open(link, "rb") as fh:
                info = parse_lnk(fh.read())
        except (OSError, struct.error, ValueError, IndexError):
            continue
        if not info:
            continue
        name = os.path.splitext(os.path.basename(link))[0]
        target = info.get("target")
        exe = unix_path(prefix, target) if target else None
        if not exe and info.get("relative_path"):
            exe = os.path.realpath(os.path.join(os.path.dirname(link), info["relative_path"].replace("\\", "/")))
        if not exe or not exe.lower().endswith(".exe") or not os.path.isfile(exe):
            continue
        if NOT_A_LAUNCHER.search(name) or NOT_A_LAUNCHER.search(os.path.basename(exe)):
            continue
        if exe in seen:
            continue
        seen.add(exe)
        args = info.get("arguments") or ""
        out.append((name, exe, args.split() if args else []))
    return out


def offer_shortcuts(ui, prefix, programs):
    rows = [(str(i), name, exe.split("/drive_c/", 1)[-1]) for i, (name, exe, _a) in enumerate(programs)]
    chosen = ui.pick_many("The installer added these programs. Add them to your Applications menu?", rows)
    made = []
    for key in chosen:
        name, exe, args = programs[int(key)]
        create_shortcut(exe, name=name, args=args, prefix=prefix)
        made.append(name)
    if made:
        ui.notify("Added to Applications", ", ".join(made))


def cmd_run(ui, target, proton_name=None, args=()):
    if not target:
        target = ui.pick_file() if ui.gui else None
        if not target:
            raise LauncherError("Choose a Windows program (.exe, .msi or .bat) to run.")
    target = os.path.abspath(target)
    if not os.path.isfile(target):
        raise LauncherError("File not found: {}".format(target))
    if os.path.splitext(target)[1].lower() not in SUPPORTED_EXTENSIONS:
        raise LauncherError("{} is not a Windows program. Supported: {}".format(
            os.path.basename(target), ", ".join(SUPPORTED_EXTENSIONS)))

    require_display()
    config = load_config()
    prefix = prefix_for(target)
    meta = load_meta(prefix)
    proton, note = choose_proton(proton_name or meta.get("proton"), config)
    if note:
        ui.notify("Proton changed", note)
    if proton_name:
        meta["proton"] = proton.name  # an explicit pick sticks to this program
    meta.setdefault("name", os.path.splitext(os.path.basename(target))[0])
    meta.setdefault("created_for", target)
    meta["last_run"] = target
    save_meta(prefix, meta)

    name = os.path.basename(target)
    if not prefix_exists(prefix):
        ui.notify("Setting up " + name, "First launch builds a fresh Windows prefix, this can take a minute.")
    before = start_menu_links(prefix)
    rc, log_path = _proton_call(proton, prefix, build_command(proton, prefix, target, args), config,
                                cwd=os.path.dirname(target), log_title="run " + target)

    new_links = start_menu_links(prefix) - before
    looks_like_installer = target.lower().endswith(".msi") or INSTALLER_HINT.search(name)
    if not new_links and looks_like_installer:
        # Some installers hand off to a child process and exit early.
        wait_for_prefix_idle(proton, prefix, config, timeout=3600)
        new_links = start_menu_links(prefix) - before
    programs = programs_from_links(prefix, new_links)
    if programs:
        offer_shortcuts(ui, prefix, programs)

    if rc not in QUIET_EXIT_CODES:
        ui.notify("{} exited with code {}".format(name, rc), "Log: " + log_path, urgent=True)
    return rc


def require_display():
    # Without a display Wine's desktop process waits forever and so does Proton.
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise LauncherError("No graphical session found (DISPLAY is not set). Run this from your desktop.")


def ensure_prefix(ui, proton, prefix, config):
    if prefix_exists(prefix):
        return
    ui.notify("Creating Windows prefix", "This can take a minute.")
    _proton_call(proton, prefix, [proton.script, "run", "wineboot"], config, cwd=prefix, log_title="create prefix")
    wait_for_prefix_idle(proton, prefix, config, timeout=600)
    if not prefix_exists(prefix):
        raise LauncherError("Proton could not create the prefix. See the log in " + LOG_DIR)


def _proton_for_prefix(prefix, config):
    proton, _note = choose_proton(load_meta(prefix).get("proton"), config)
    return proton


def cmd_winecfg(ui, target):
    require_display()
    config = load_config()
    prefix = prefix_for(target)
    proton = _proton_for_prefix(prefix, config)
    os.makedirs(prefix, exist_ok=True)
    ensure_prefix(ui, proton, prefix, config)
    rc, _log = _proton_call(proton, prefix, [proton.script, "run", os.path.join(system32(prefix), "winecfg.exe")],
                            config, cwd=prefix, log_title="winecfg")
    return rc


def cmd_winetricks(ui, target, verbs):
    winetricks = shutil.which("winetricks")
    if not winetricks:
        raise LauncherError("winetricks is not installed. Get it from your package manager (package name: winetricks).")
    require_display()
    config = load_config()
    prefix = prefix_for(target)
    proton = _proton_for_prefix(prefix, config)
    os.makedirs(prefix, exist_ok=True)
    ensure_prefix(ui, proton, prefix, config)
    env = proton_env(prefix, config)
    env.update({
        "WINEPREFIX": os.path.join(prefix, "pfx"),
        "WINE": proton.bin("wine") or "wine",
        "WINESERVER": proton.bin("wineserver") or "wineserver",
        # Proton launches through us; stop Wine making menu entries that use system Wine.
        "WINEDLLOVERRIDES": ";".join(filter(None, [env.get("WINEDLLOVERRIDES"), "winemenubuilder.exe=d"])),
    })
    return subprocess.call([winetricks] + (list(verbs) or ["--gui"]), env=env)


def cmd_open_prefix(ui, target):
    prefix = prefix_for(target)
    if not prefix_exists(prefix):
        raise LauncherError("{} has not been run yet, so it has no C: drive.".format(os.path.basename(target)))
    opener = shutil.which("gio")
    argv = [opener, "open", drive_c(prefix)] if opener else ["xdg-open", drive_c(prefix)]
    return subprocess.call(argv)


def cmd_delete_prefix(ui, target, assume_yes=False):
    prefix = prefix_for(target)
    if not os.path.isdir(prefix):
        ui.notify("Nothing to delete", "{} has no prefix yet.".format(os.path.basename(target)))
        return 0
    shortcuts = shortcuts_for_prefix(prefix)
    question = ("Delete the Windows prefix for {}?\n\nThis removes everything installed into it, "
                "including saves and settings stored there{}.").format(
        os.path.basename(target),
        ", and {} Applications menu shortcut(s)".format(len(shortcuts)) if shortcuts else "")
    if not assume_yes and not ui.confirm(question):
        return 1
    for path in shortcuts:
        os.remove(path)
    for name in os.listdir(ICON_DIR) if os.path.isdir(ICON_DIR) else []:
        if name.startswith("{}-{}-".format(APP_ID, os.path.basename(prefix))):
            os.remove(os.path.join(ICON_DIR, name))
    shutil.rmtree(prefix)
    ui.notify("Prefix deleted", os.path.basename(target) + " will start fresh next time.")
    return 0


def cmd_shortcut(ui, target, name=None):
    target = os.path.abspath(target)
    if not os.path.isfile(target):
        raise LauncherError("File not found: {}".format(target))
    if name is None:
        name = ui.ask_text("Name in the Applications menu:", os.path.splitext(os.path.basename(target))[0])
        if not name:
            return 1
    path = create_shortcut(target, name=name)
    ui.notify("Added to Applications", name)
    print(path)
    return 0


def cmd_list(_ui, names_only=False):
    protons = find_protons()
    if not protons:
        print("No Proton builds found. Install \"Proton Experimental\" from your Steam library (Tools).")
        return 1
    default = default_proton_name(protons)
    width = max(len(p.name) for p in protons)
    for p in protons:
        mark = "*" if p.name == default else " "
        print("{} {}".format(mark, p.name) if names_only else "{} {:<{}}  {}".format(mark, p.name, width, p.path))
    if names_only:
        return 0
    print("\n* = default for new programs (change it in {})".format(CONFIG_FILE))
    return 0


def cmd_prefixes(_ui):
    prefixes = list_prefixes()
    if not prefixes:
        print("No prefixes yet. They live in " + PREFIX_ROOT)
        return 0
    for prefix in prefixes:
        meta = load_meta(prefix)
        print("{}\n    program: {}\n    proton:  {}".format(
            os.path.basename(prefix), meta.get("created_for", "?"), meta.get("proton", "(default)")))
    return 0


def cmd_menu(ui, target):
    """Action picker used by the plain Nautilus-scripts integration."""
    prefix = prefix_for(target)
    rows = [("run", "Run"), ("version", "Run with a specific Proton version…"), ("shortcut", "Add to Applications menu")]
    if prefix_exists(prefix):
        rows.append(("open", "Browse its C: drive"))
    rows.append(("winecfg", "Wine configuration"))
    if shutil.which("winetricks"):
        rows.append(("winetricks", "Winetricks (install runtimes and fonts)"))
    if os.path.isdir(prefix):
        rows.append(("delete", "Delete its prefix…"))
    choice = ui.pick_one(os.path.basename(target), rows)
    if choice == "run":
        return cmd_run(ui, target)
    if choice == "version":
        current = load_meta(prefix).get("proton")
        name = ui.pick_one("Run {} with:".format(os.path.basename(target)),
                           [(p.name, p.name + ("  (current)" if p.name == current else "")) for p in find_protons()])
        return cmd_run(ui, target, proton_name=name) if name else 1
    if choice == "shortcut":
        return cmd_shortcut(ui, target)
    if choice == "open":
        return cmd_open_prefix(ui, target)
    if choice == "winecfg":
        return cmd_winecfg(ui, target)
    if choice == "winetricks":
        return cmd_winetricks(ui, target, [])
    if choice == "delete":
        return cmd_delete_prefix(ui, target)
    return 1


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

COMMANDS = ("run", "list", "prefixes", "winecfg", "winetricks", "open-prefix",
            "delete-prefix", "shortcut", "menu", "config")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="proton-launch",
        description="Run Windows programs with Steam's Proton. `proton-launch FILE` is short for `proton-launch run FILE`.")
    parser.add_argument("--version", action="version", version="%(prog)s " + __version__)
    parser.add_argument("--gui", action="store_true", help="report through desktop notifications and dialogs")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("run", help="run a .exe, .msi or .bat (program arguments go after --)")
    p.add_argument("file", nargs="?")
    p.add_argument("--proton", metavar="NAME", help="use (and remember) this Proton build for the program")

    sub.add_parser("list", help="show installed Proton builds").add_argument(
        "--names", action="store_true", help="names only")
    sub.add_parser("prefixes", help="show the prefixes Proton Launcher has made")
    sub.add_parser("config", help="create the config file if needed and print its path")
    for name, text in (("winecfg", "open Wine settings for a program's prefix"),
                       ("open-prefix", "open a program's C: drive in the file manager"),
                       ("menu", "pick an action from a dialog")):
        sub.add_parser(name, help=text).add_argument("file")
    p = sub.add_parser("winetricks", help="run winetricks in a program's prefix")
    p.add_argument("file")
    p.add_argument("verbs", nargs="*", help="e.g. vcrun2022 corefonts (no verbs opens the GUI)")
    p = sub.add_parser("delete-prefix", help="delete a program's prefix")
    p.add_argument("file")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask")
    p = sub.add_parser("shortcut", help="add a program to the Applications menu")
    p.add_argument("file")
    p.add_argument("--name")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    program_args = []
    if "--" in argv:
        split = argv.index("--")
        argv, program_args = argv[:split], argv[split + 1:]
    # `proton-launch game.exe` works like `proton-launch run game.exe`.
    first = next((i for i, a in enumerate(argv) if a != "--gui"), None)
    if first is not None and argv[first] not in COMMANDS and not argv[first].startswith("-"):
        argv.insert(first, "run")
    opts = build_parser().parse_args(argv)
    ui = UI(opts.gui)
    try:
        if opts.command in (None, "run"):
            if opts.command is None and not opts.gui:
                build_parser().print_help()
                return 2
            return cmd_run(ui, getattr(opts, "file", None), getattr(opts, "proton", None), program_args)
        if opts.command == "list":
            return cmd_list(ui, opts.names)
        if opts.command == "prefixes":
            return cmd_prefixes(ui)
        if opts.command == "config":
            print(ensure_config_file())
            return 0
        if opts.command == "winecfg":
            return cmd_winecfg(ui, opts.file)
        if opts.command == "winetricks":
            return cmd_winetricks(ui, opts.file, opts.verbs)
        if opts.command == "open-prefix":
            return cmd_open_prefix(ui, opts.file)
        if opts.command == "delete-prefix":
            return cmd_delete_prefix(ui, opts.file, opts.yes)
        if opts.command == "shortcut":
            return cmd_shortcut(ui, opts.file, opts.name)
        if opts.command == "menu":
            return cmd_menu(ui, opts.file)
    except LauncherError as exc:
        ui.error(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
