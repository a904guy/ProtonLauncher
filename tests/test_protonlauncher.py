import glob
import importlib.util
import os
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_HOME = os.path.expanduser("~")
spec = importlib.util.spec_from_file_location("protonlauncher", os.path.join(HERE, "..", "src", "protonlauncher.py"))
pl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pl)


def touch(path, data=b""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


class TempHome(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("PREFIX_ROOT", os.path.join(self.home, "prefixes")),
                            ("APPS_DIR", os.path.join(self.home, "apps")),
                            ("ICON_DIR", os.path.join(self.home, "icons")),
                            ("CONFIG_FILE", os.path.join(self.home, "config.ini"))):
            p = mock.patch.object(pl, name, value)
            p.start()
            self.addCleanup(p.stop)


class ProtonDiscovery(TempHome):
    def make_proton(self, base, name, layout="files"):
        touch(os.path.join(base, name, "proton"))
        touch(os.path.join(base, name, layout, "bin", "wine"))

    def test_finds_protons_across_libraries_and_ranks_them(self):
        steam = os.path.join(self.home, ".local/share/Steam")
        other_lib = os.path.join(self.home, "games")
        os.makedirs(os.path.join(other_lib, "steamapps", "common"))
        touch(os.path.join(steam, "steamapps", "libraryfolders.vdf"), (
            '"libraryfolders"\n{\n "0"\n {\n  "path"\t\t"%s"\n }\n "1"\n {\n  "path"\t\t"%s"\n }\n}\n'
            % (steam, other_lib)).encode())
        common = os.path.join(steam, "steamapps", "common")
        self.make_proton(common, "Proton 9.0 (Beta)")
        self.make_proton(common, "Proton 3.16", layout="dist")
        self.make_proton(os.path.join(other_lib, "steamapps", "common"), "Proton 11.0")
        self.make_proton(common, "Proton - Experimental")
        self.make_proton(os.path.join(steam, "compatibilitytools.d"), "GE-Proton10-25")
        touch(os.path.join(common, "Proton BattlEye Runtime", "README"))
        touch(os.path.join(common, "Not Proton", "proton"))  # no wine inside

        names = [p.name for p in pl.find_protons()]
        self.assertEqual(names, ["Proton - Experimental", "Proton 11.0", "Proton 9.0 (Beta)",
                                 "Proton 3.16", "GE-Proton10-25"])

    def test_config_overrides_default_and_missing_choice_falls_back(self):
        common = os.path.join(self.home, ".local/share/Steam/steamapps/common")
        self.make_proton(common, "Proton - Experimental")
        self.make_proton(common, "Proton 10.0")
        with open(pl.CONFIG_FILE, "w") as fh:
            fh.write("[launcher]\nproton = Proton 10.0\n[env]\nDXVK_HUD = fps\n")
        config = pl.load_config()
        self.assertEqual(config["env"], {"DXVK_HUD": "fps"})
        self.assertEqual(pl.choose_proton(None, config)[0].name, "Proton 10.0")
        proton, note = pl.choose_proton("Proton 7.0", config)
        self.assertEqual(proton.name, "Proton 10.0")
        self.assertIn("Proton 7.0", note)

    def test_no_proton_is_a_friendly_error(self):
        with self.assertRaises(pl.LauncherError):
            pl.choose_proton()

    def test_vdf_paths_unescape(self):
        self.assertEqual(pl.parse_library_paths('"path"  "/mnt/My \\"Games\\""'), ['/mnt/My "Games"'])


class Prefixes(TempHome):
    def test_each_file_gets_its_own_prefix(self):
        a = pl.prefix_for("/home/u/Downloads/setup.exe")
        b = pl.prefix_for("/home/u/Downloads/other setup.exe")
        self.assertNotEqual(a, b)
        self.assertTrue(os.path.basename(a).startswith("setup-"))
        self.assertEqual(a, pl.prefix_for("/home/u/Downloads/setup.exe"))

    def test_programs_installed_inside_a_prefix_reuse_it(self):
        prefix = pl.prefix_for("/home/u/Downloads/setup.exe")
        installed = os.path.join(prefix, "pfx/drive_c/Program Files/App/app.exe")
        touch(installed)
        self.assertEqual(pl.prefix_for(installed), prefix)

    def test_windows_paths(self):
        self.assertEqual(pl.windows_path("/home/u/My App/a.msi"), "Z:\\home\\u\\My App\\a.msi")
        prefix = os.path.join(pl.PREFIX_ROOT, "demo")
        exe = os.path.join(prefix, "pfx/drive_c/Program Files/Demo App/Demo.exe")
        touch(exe)
        os.makedirs(os.path.join(prefix, "pfx/dosdevices"))
        os.symlink("../drive_c", os.path.join(prefix, "pfx/dosdevices/c:"))
        self.assertEqual(pl.unix_path(prefix, "C:\\program files\\DEMO APP\\demo.exe"), os.path.realpath(exe))
        self.assertIsNone(pl.unix_path(prefix, "C:\\nope\\x.exe"))

    def test_commands_route_msi_and_bat_through_full_paths(self):
        proton = pl.Proton("P", "/opt/p")
        prefix = "/pfx"
        self.assertEqual(pl.build_command(proton, prefix, "/a/b.exe", ["-x"]), ["/opt/p/proton", "run", "/a/b.exe", "-x"])
        msi = pl.build_command(proton, prefix, "/a/b.MSI")
        self.assertEqual(msi[2:], ["/pfx/pfx/drive_c/windows/system32/msiexec.exe", "/i", "Z:\\a\\b.MSI"])
        bat = pl.build_command(proton, prefix, "/a/go.bat")
        self.assertEqual(bat[2:], ["/pfx/pfx/drive_c/windows/system32/cmd.exe", "/c", "Z:\\a\\go.bat"])


class WindowsFormats(TempHome):
    def test_parse_wine_written_shortcut(self):
        with open(os.path.join(HERE, "fixtures", "demo-app.lnk"), "rb") as fh:
            info = pl.parse_lnk(fh.read())
        self.assertEqual(info["target"], "C:\\Program Files\\Demo App\\Demo.exe")
        self.assertEqual(info["arguments"], "--fast")
        self.assertEqual(info["working_dir"], "C:\\Program Files\\Demo App")

    def test_programs_from_links_skips_uninstallers(self):
        prefix = os.path.join(pl.PREFIX_ROOT, "demo")
        touch(os.path.join(prefix, "pfx/drive_c/Program Files/Demo App/Demo.exe"))
        os.makedirs(os.path.join(prefix, "pfx/dosdevices"))
        os.symlink("../drive_c", os.path.join(prefix, "pfx/dosdevices/c:"))
        menu = os.path.join(prefix, "pfx/drive_c/ProgramData/Microsoft/Windows/Start Menu/Programs/Demo")
        os.makedirs(menu)
        fixture = os.path.join(HERE, "fixtures", "demo-app.lnk")
        shutil.copy(fixture, os.path.join(menu, "Demo App.lnk"))
        shutil.copy(fixture, os.path.join(menu, "Uninstall Demo.lnk"))
        links = pl.start_menu_links(prefix)
        self.assertEqual(len(links), 2)
        programs = pl.programs_from_links(prefix, links)
        self.assertEqual([(n, a) for n, _e, a in programs], [("Demo App", ["--fast"])])

    def test_not_a_pe_file(self):
        exe = os.path.join(self.home, "fake.exe")
        touch(exe, b"MZ not really")
        self.assertIsNone(pl.extract_icon(exe, os.path.join(self.home, "icon")))

    def test_icon_from_real_windows_program(self):
        candidates = glob.glob(os.path.join(
            REAL_HOME, ".local/share/Steam/steamapps/common/Proton*/files/lib/wine/x86_64-windows/winecfg.exe"))
        if not candidates:
            self.skipTest("no Proton installed to borrow an .exe from")
        path = pl.extract_icon(candidates[0], os.path.join(self.home, "icon"))
        self.assertTrue(path and os.path.getsize(path) > 100)


class DesktopEntries(TempHome):
    def test_exec_quoting(self):
        self.assertEqual(pl.desktop_exec_arg("/a b/c.exe"), '"/a b/c.exe"')
        self.assertEqual(pl.desktop_exec_arg('/x/$y"z'), '"/x/\\\\$y\\\\"z"')
        self.assertEqual(pl.desktop_exec_arg("100%"), '"100%%"')

    def test_shortcut_round_trip(self):
        exe = os.path.join(self.home, "Games", "My Game", "game.exe")
        touch(exe, b"MZ")
        path = pl.create_shortcut(exe, name="My Game", args=["-windowed"])
        with open(path) as fh:
            text = fh.read()
        self.assertIn("Name=My Game\n", text)
        self.assertIn("StartupWMClass=game.exe\n", text)
        self.assertIn('"run" "{}" "--" "-windowed"'.format(exe), text)
        self.assertEqual(pl.shortcuts_for_prefix(pl.prefix_for(exe)), [path])


class CommandLine(unittest.TestCase):
    def test_bare_file_means_run(self):
        with mock.patch.object(pl, "cmd_run", return_value=0) as run:
            self.assertEqual(pl.main(["--gui", "game.exe"]), 0)
            run.assert_called_once()
            self.assertEqual(run.call_args[0][1:], ("game.exe", None, []))

    def test_program_arguments_after_double_dash(self):
        with mock.patch.object(pl, "cmd_run", return_value=0) as run:
            pl.main(["run", "game.exe", "--proton", "Proton 10.0", "--", "-dx11", "--safe"])
            self.assertEqual(run.call_args[0][1:], ("game.exe", "Proton 10.0", ["-dx11", "--safe"]))

    def test_rejects_non_windows_files(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as tmp, \
                mock.patch.object(pl.UI, "error") as error:
            self.assertEqual(pl.main(["run", tmp.name]), 1)
            self.assertIn("not a Windows program", error.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
