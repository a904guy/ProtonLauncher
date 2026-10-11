<p align="center">
  <img src="data/protonlauncher.svg" width="112" alt="Proton Launcher icon">
</p>

<h1 align="center">Proton Launcher</h1>

<p align="center">
  <b>Right-click any Windows program in GNOME Files → <i>Run with Proton</i>.</b><br>
  Uses the Proton that Steam already installs, so it updates itself.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Linux-blue" alt="Linux">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT license">
</p>

---

Proton is Valve's tuned build of Wine. It's what lets Steam run Windows games on
Linux, and it's very good at running ordinary Windows programs too. The catch is
that Steam only uses it for games in your Steam library. Proton Launcher lets you
run **any** `.exe`, `.msi` or `.bat` with it: an installer you downloaded, a
DRM-free game, a portable tool, an old utility.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/a904guy/ProtonLauncher/main/install.sh | bash
```

Or from a clone:

```bash
git clone https://github.com/a904guy/ProtonLauncher.git
cd ProtonLauncher
./install.sh
```

It installs for your user only and needs **no sudo**. Run it again at any time to
update.

**You need:**

- **Steam** with at least one Proton installed. *Proton Experimental* is the
  recommended one: in Steam, open **Library**, search for "Proton Experimental"
  (it's listed under **Tools**) and install it. GE-Proton and other custom builds
  in `compatibilitytools.d` are picked up too. Native, Flatpak and Snap Steam all
  work.
- **python3-nautilus** to get the right-click item in GNOME Files. Most GNOME
  distros already ship it. Without it, the installer falls back to the
  **Scripts** submenu.
  `sudo apt install python3-nautilus` · `sudo dnf install nautilus-python` · `sudo pacman -S python-nautilus`
- Optional: `zenity` for dialogs (usually preinstalled) and `winetricks`.

Installer options:

| Option | What it does |
| --- | --- |
| `--default` | Make Proton Launcher the double-click app for `.exe`/`.msi` files |
| `--scripts` | Use the Files *Scripts* menu even if the extension could be used |

## Using it

**Right-click a `.exe`, `.msi` or `.bat` in Files:**

```
Run with Proton
Proton Launcher ▸  Run With Version ▸  Proton - Experimental ✓
                                       Proton 10.0
                                       GE-Proton10-25 …
                   Add to Applications Menu
                   Browse C: Drive
                   Wine Settings
                   Winetricks
                   Delete Prefix…
```

- **Run with Proton**: starts the program. The first launch takes a minute
  while Proton sets up the program's Windows environment.
- **Run With Version**: picks a different Proton build. The choice is
  remembered for that program.
- **Add to Applications Menu**: adds the program to your app grid, with the
  icon taken from the `.exe`.
- **Browse C: Drive**: opens the program's private Windows drive in Files.
- **Wine Settings / Winetricks**: tweak the program's environment or install
  runtimes such as .NET, Visual C++ and core fonts.
- **Delete Prefix**: wipes the program's environment so it starts fresh.

**Installers work the way you'd hope.** Run a `setup.exe` and Proton Launcher
watches for the Start Menu shortcuts the installer creates. When it finishes,
you get a checklist of the programs it installed, and the ones you tick are
added to your Applications menu.

**Other file managers** (Nemo, Thunar, Dolphin…): use **Open With → Proton
Launcher**, or install with `--default` so double-clicking just works.

**Terminal:**

```bash
proton-launch game.exe                     # run it
proton-launch run setup.exe -- /S          # pass arguments after --
proton-launch run app.exe --proton "Proton 10.0"
proton-launch list                         # installed Proton builds
proton-launch prefixes                     # environments created so far
proton-launch winetricks app.exe vcrun2022 corefonts
proton-launch shortcut app.exe --name "My App"
proton-launch --help
```

## What works and what doesn't

Proton Launcher runs Proton the same way Steam does, without Steam itself in the
middle. That's great for most software, with a few hard limits.

**Works well**

- Everyday Windows programs, including installers and portable apps: Notepad++,
  7-Zip, IrfanView, foobar2000, older Office versions (2007-2013, results vary),
  and most small utilities.
- DRM-free games: GOG installers, itch.io downloads, old CD-ROM games, fan
  patches, mod managers.
- .NET programs: Proton ships Wine Mono, and **Winetricks** can install the real
  .NET Framework when an app insists.

**Won't work, and no launcher can fix it**

- **Anything that installs a Windows driver**: VPN clients, antivirus, RGB and
  peripheral tools, printer and scanner utilities, phone flashing tools.
- **Kernel-level anti-cheat**, and games that need the EasyAntiCheat/BattlEye
  runtime from Steam.
- **Steam games with Steam DRM**. They expect to be launched by Steam. Add them
  to Steam instead (*Add a Non-Steam Game* also works for anything you want the
  Steam overlay or controller support on).
- Modern Microsoft 365, Adobe Creative Cloud, and other apps with heavy
  licensing services. Older versions are often fine.
- Shell extensions and Explorer add-ons.

Not sure about a program? Check the [WineHQ AppDB](https://appdb.winehq.org/)
for apps or [ProtonDB](https://www.protondb.com/) for games.

## How it works

- **One prefix per program.** Each program gets its own Windows environment
  (a Wine *prefix*: a private `C:` drive and registry) in
  `~/.local/share/protonlauncher/prefixes/`, so a runtime one app needs can't
  break another. Programs an installer puts *inside* a prefix reuse that prefix
  automatically. Saves and settings a program keeps in its Windows user folders
  live there too, so **Delete Prefix** removes them as well. A prefix belongs
  to the file's location: if you move or rename a program it starts with a new
  prefix (delete the old one from the Files menu before moving it, or remove
  it from `~/.local/share/protonlauncher/prefixes/`).
- **Self-updating.** It runs the `proton` script from your Steam library
  directly, so when Steam updates Proton you're updated too.
- **Logs** for the last two runs of each program are in
  `~/.cache/protonlauncher/`. If something exits with an error you'll get a
  notification pointing at the log.

## Settings

Run `proton-launch config` to create and locate `~/.config/protonlauncher/config.ini`:

```ini
[launcher]
# Proton used for programs that haven't picked one (see `proton-launch list`)
proton = GE-Proton10-25
# Full Wine debug logs in ~/.cache/protonlauncher (large)
verbose_logs = no

[env]
# Extra environment variables for every program
DXVK_HUD = fps
```

## Troubleshooting

| Problem | Fix |
| --- | --- |
| No *Run with Proton* in the menu | Restart Files: `nautilus -q`. Check `python3-nautilus` is installed, or use **Scripts → Run with Proton**. |
| "No Proton found" | Install *Proton Experimental* from Steam's Library (Tools). |
| Program won't start or crashes | Read the log in `~/.cache/protonlauncher/`. Try **Run With Version** → another Proton (GE-Proton fixes a lot of media and video issues). |
| App says it needs .NET / Visual C++ | **Proton Launcher → Winetricks** → install `dotnet48` or `vcrun2022`. |
| Text or fonts look wrong | Winetricks → `corefonts`. |
| Something got messed up | **Delete Prefix…** and run it again. |

## Uninstall

```bash
~/.local/share/protonlauncher/uninstall.sh          # keeps your prefixes
~/.local/share/protonlauncher/uninstall.sh --purge  # removes everything
```

## Development

The whole engine is one dependency-free Python file (`src/protonlauncher.py`,
Python 3.8+). The Files extension (`src/protonlauncher_nautilus.py`) imports it.

```bash
python3 -m unittest discover -s tests -v
```

---

Not affiliated with or endorsed by Valve. Steam and Proton are trademarks of
Valve Corporation. Proton itself is not bundled: Proton Launcher uses the copy
Steam installs on your machine.

Released under the [MIT License](LICENSE).
