import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class InstallTests(unittest.TestCase):
    def test_launcher_does_not_import_package_from_caller_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / 'omacarry'
            package.mkdir()
            (package / '__init__.py').write_text('')
            (package / '__main__.py').write_text('raise SystemExit("wrong source tree")')
            result = subprocess.run([str(ROOT / 'bin/omacarry'), '--help'], cwd=tmp,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('usage:', result.stdout)

    def test_menu_installer_writes_launcher_for_this_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_DATA_HOME=tmp)
            result = subprocess.run([str(ROOT / 'bin/omacarry-install'), '--no-hook'],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            desktop = (Path(tmp) / 'applications/omacarry.desktop').read_text()
            self.assertIn(str(ROOT / 'bin/omacarry'), desktop)
            self.assertIn('Terminal=false', desktop)


if __name__ == '__main__':
    unittest.main()
