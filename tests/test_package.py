"""Tests d'intégrité du package HACS, de la version unique et de la parité des traductions."""
import ast
import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CC_DIR = ROOT / "custom_components" / "ha_thermostat_split"


def _flatten_keys(obj, prefix=""):
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            cur = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                keys |= _flatten_keys(v, cur)
            else:
                keys.add(cur)
    return keys


class PackageIntegrityTests(unittest.TestCase):
    def test_all_python_files_parse_cleanly(self):
        for py_file in CC_DIR.glob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            ast.parse(source, filename=str(py_file))

    def test_version_matches_manifest(self):
        manifest = json.loads((CC_DIR / "manifest.json").read_text(encoding="utf-8"))
        spec = importlib.util.spec_from_file_location("const", CC_DIR / "const.py")
        const_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(const_mod)
        self.assertEqual(manifest["version"], const_mod.VERSION)

    def test_translations_parity(self):
        strings = _flatten_keys(json.loads((CC_DIR / "strings.json").read_text(encoding="utf-8")))
        fr = _flatten_keys(
            json.loads((CC_DIR / "translations" / "fr.json").read_text(encoding="utf-8"))
        )
        en = _flatten_keys(
            json.loads((CC_DIR / "translations" / "en.json").read_text(encoding="utf-8"))
        )
        self.assertEqual(strings, fr)
        self.assertEqual(strings, en)


if __name__ == "__main__":
    unittest.main()
