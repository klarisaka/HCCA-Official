import importlib.util
from pathlib import Path
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hcca_installer", PLUGIN / "install.py")
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class InstallTest(unittest.TestCase):
    def test_preserves_other_entries_and_updates_owned_stanzas_once(self):
        original = (
            "# user settings\n- insert:\n"
            "    - id: unrelated\n      name: user-plugin\n"
            "    - id: hcca-host\n      name: old-host\n      config:\n        port: 9999\n"
            "    - id: keep-also\n      name: second-plugin\n"
            "- insert:\n    - id: preset-hcca\n      config:\n        id: hcca\n"
            "- remove:\n    - user-disabled-plugin\n"
        )
        managed = INSTALL.managed_patch(PLUGIN, Path('/source/repo'), '/usr/bin/python3', 8766)
        once = INSTALL.remove_old_entries(original) + '\n\n' + managed
        twice = INSTALL.remove_old_entries(once) + '\n\n' + managed
        self.assertEqual(once, twice)
        self.assertEqual(once.count('id: hcca-host'), 1)
        self.assertEqual(once.count('id: preset-hcca'), 1)
        self.assertIn('id: unrelated\n      name: user-plugin', once)
        self.assertIn('id: keep-also\n      name: second-plugin', once)
        self.assertIn('- remove:\n    - user-disabled-plugin', once)
        self.assertIn('port: 8766', once)
        self.assertNotIn('port: 9999', once)


if __name__ == '__main__':
    unittest.main()
