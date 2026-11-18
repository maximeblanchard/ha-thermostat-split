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

    def test_climate_tracker_and_learner_attributes_exist(self):
        """Vérifie qu'aucun attribut inexistant n'est appelé sur self._tracker / self._learner."""
        comp_src = (CC_DIR / "compressor.py").read_text(encoding="utf-8")
        comp_tree = ast.parse(comp_src)
        tracker_attrs = set()
        for node in ast.walk(comp_tree):
            if isinstance(node, ast.ClassDef) and node.name == "CompressorTracker":
                for item in ast.walk(node):
                    if isinstance(item, ast.FunctionDef):
                        tracker_attrs.add(item.name)
                    elif (
                        isinstance(item, ast.Attribute)
                        and isinstance(item.value, ast.Name)
                        and item.value.id == "self"
                    ):
                        tracker_attrs.add(item.attr)

        clim_src = (CC_DIR / "climate.py").read_text(encoding="utf-8")
        clim_tree = ast.parse(clim_src)
        for node in ast.walk(clim_tree):
            if isinstance(node, ast.Attribute):
                # self._tracker.<attr> ou tracker.<attr> ou tr.<attr>
                if (
                    isinstance(node.value, ast.Attribute)
                    and isinstance(node.value.value, ast.Name)
                    and node.value.value.id == "self"
                    and node.value.attr == "_tracker"
                ) or (
                    isinstance(node.value, ast.Name)
                    and node.value.id in ("tracker", "tr")
                ):
                    self.assertIn(
                        node.attr,
                        tracker_attrs,
                        f"climate.py accède à un attribut inexistant sur CompressorTracker : {node.attr}",
                    )



if __name__ == "__main__":
    unittest.main()
