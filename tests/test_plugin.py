# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests of the OctoPrint plugin (no server: simulated settings)."""
import json
import logging
import os
import re
import threading

import octoprint.slicing
import pytest

import octoprint_curaengine5 as plugin_module
from octoprint_curaengine5.resolver.containers import parse_cfg
from octoprint_curaengine5.slicing import stl

from .conftest import DATA, cura_appdir, cura_resources


class FakeSettings:
    def __init__(self, values):
        self.values = values

    def get(self, path, merged=False):
        node = self.values
        for p in path:
            node = node.get(p) if isinstance(node, dict) else None
        return node

    def get_int(self, path):
        v = self.get(path)
        return int(v) if v not in (None, "") else None

    def get_boolean(self, path):
        return bool(self.get(path))

    def set(self, path, value):
        node = self.values
        for p in path[:-1]:
            node = node.setdefault(p, {})
        node[path[-1]] = value

    def save(self):
        pass

    def global_get(self, path):
        return {"pathSuffix": "path", "nameSuffix": "name"}.get(path[-1])


@pytest.fixture
def plugin(tmp_path):
    engine_path = os.environ.get("CURAENGINE5_ENGINE", os.path.join(cura_appdir(), "CuraEngine"))
    if not os.path.exists(engine_path) or not os.path.isdir(cura_resources()):
        pytest.skip("CuraEngine / Cura resources not found")
    p = plugin_module.CuraEngine5Plugin()
    defaults = p.get_settings_defaults()
    with open(os.path.join(DATA, "eryone_thinker_definition_changes.inst.cfg"), encoding="utf-8") as f:
        dc = parse_cfg(f.read())[2]
    defaults.update({
        "engine_path": engine_path,
        "resources_path": cura_resources(),
        "machine": {"definition": "eryone_thinker", "definition_changes": dc,
                    "extruders": [{"material": "generic_abs", "variant": "", "definition_changes": {}}]},
    })
    p._settings = FakeSettings(defaults)
    p._logger = logging.getLogger("test.curaengine5")
    p.get_plugin_data_folder = lambda: str(tmp_path / "data")
    (tmp_path / "data").mkdir()
    return p


def _import_abs(plugin):
    from octoprint_curaengine5.resolver.profiles import read_curaprofile

    imported = read_curaprofile(os.path.join(DATA, "eryone_thinker_esun_abs.curaprofile"))
    return plugin._profile_from_import(imported, material="generic_abs")


def test_properties_and_configured(plugin):
    props = plugin.get_slicer_properties()
    assert props["type"] == "curaengine5" and props["same_device"] is True
    assert props["source_file_types"] == ["stl"]
    assert plugin.is_slicer_configured()


def test_profile_roundtrip_with_overrides(plugin, tmp_path):
    data = _import_abs(plugin)
    profile = octoprint.slicing.SlicingProfile("curaengine5", "abs", data, display_name="ABS", description="test")
    path = str(tmp_path / "abs.profile")
    plugin.save_slicer_profile(path, profile, overrides={"layer_height": 0.3, "support_enable": False,
                                                          "extruder.0.speed_print": 33})
    loaded = plugin.get_slicer_profile(path)
    assert loaded.display_name == "ABS" and loaded.description == "test"
    assert loaded.data["quality_type"] == "normal"
    assert loaded.data["machine"] == "eryone_thinker"
    assert loaded.data["extruders"][0]["material_print_temperature"] == "240"
    assert loaded.data["user"] == {"layer_height": "0.3", "support_enable": "False"}
    assert loaded.data["user_extruders"] == [{"speed_print": "33"}]
    with pytest.raises(IOError):
        plugin.save_slicer_profile(path, profile, allow_overwrite=False)


def test_settings_machine_json(plugin):
    plugin._settings.values.pop("machine_json", None)
    machine = plugin._validate_machine({"definition": "custom", "definition_changes": {"machine_width": 220, "machine_heated_bed": True},
                                        "extruders": [{"material": "generic_pla", "definition_changes": {"machine_nozzle_size": 0.6}}]})
    assert machine == {"definition": "custom", "definition_changes": {"machine_width": "220", "machine_heated_bed": "True"},
                       "extruders": [{"material": "generic_pla", "variant": "", "definition_changes": {"machine_nozzle_size": "0.6"}}]}
    with pytest.raises(ValueError):
        plugin._validate_machine([])


def test_do_slice_with_progress(plugin, tmp_path):
    data = _import_abs(plugin)
    profile = octoprint.slicing.SlicingProfile("curaengine5", "abs", data)
    profile_path = str(tmp_path / "abs.profile")
    plugin.save_slicer_profile(profile_path, profile, overrides={"layer_height": "0.3"})
    model = str(tmp_path / "cube.stl")
    stl.write_box(model, 15, 15, 9)
    out = str(tmp_path / "cube.gcode")
    progress = []
    printer_profile = {"volume": {"width": 200, "depth": 200, "height": 200, "origin": "lowerleft"}}
    ok, result = plugin.do_slice(model, printer_profile, machinecode_path=out, profile_path=profile_path,
                                 position={"x": 150, "y": 150},
                                 on_progress=lambda *a, **kw: progress.append(kw["_progress"]))
    assert ok, result
    text = open(out, encoding="utf-8").read()
    assert ";Layer height: 0.3" in text
    # first layer of 0.1 mm (ABS profile) then (9 - 0.1) / 0.3 = about 30 layers
    assert len(re.findall(r"^;LAYER:\d+$", text, re.M)) == 31
    assert result["analysis"]["estimatedPrintTime"] > 0
    assert progress[-1] == 1.0
    # "auto" mode: the eryone_thinker definition keeps its 300 mm volume (not the OctoPrint profile's)
    assert ";MAXX:" in text


def test_do_slice_without_profile_uses_default(plugin, tmp_path):
    model = str(tmp_path / "cube.stl")
    stl.write_box(model, 10, 10, 2)
    out = str(tmp_path / "d.gcode")
    ok, result = plugin.do_slice(model, None, machinecode_path=out)
    assert ok, result
    assert ";LAYER:0" in open(out, encoding="utf-8").read()


def test_cancel_slicing(plugin, tmp_path):
    model = str(tmp_path / "cube.stl")
    stl.write_box(model, 60, 60, 60)
    out = str(tmp_path / "c.gcode")
    started = threading.Event()

    def on_progress(*a, **kw):
        started.set()

    def cancel():
        started.wait(30)
        plugin.cancel_slicing(out)

    t = threading.Thread(target=cancel)
    t.start()
    with pytest.raises(octoprint.slicing.SlicingCancelled):
        plugin.do_slice(model, None, machinecode_path=out, on_progress=on_progress)
    t.join()
    assert not os.path.exists(out)


def test_missing_engine_reports_error(plugin, tmp_path):
    plugin._settings.values["engine_path"] = str(tmp_path / "nope")
    ok, message = plugin.do_slice(str(tmp_path / "x.stl"), None, machinecode_path=str(tmp_path / "x.gcode"))
    assert not ok and "CuraEngine" in message


# --- Printers ---------------------------------------------------------

def test_new_printer_defaults_and_octoprint_profile(plugin):
    from octoprint_curaengine5 import printers as pm

    library = plugin._get_library()
    printer = pm.new_printer(library, "eryone_thinker", existing_ids=[])
    assert printer["definition"] == "eryone_thinker" and printer["id"] == "eryone_thinker_series"
    settings = pm.machine_settings(library, printer)
    assert settings["global"]["machine_width"]["value"] == settings["global"]["machine_width"]["default"]
    printer = pm.apply_machine_settings(library, printer, {"machine_width": "250", "machine_heated_bed": False},
                                        {"machine_nozzle_size": "0.6"}, name="My Thinker")
    assert printer["definition_changes"]["machine_width"] == "250"
    assert printer["extruders"][0]["definition_changes"]["machine_nozzle_size"] == "0.6"
    # a value identical to the definition is not stored
    base = settings["global"]["machine_depth"]["default"]
    printer = pm.apply_machine_settings(library, printer, {"machine_depth": base})
    assert "machine_depth" not in printer["definition_changes"]
    op = pm.octoprint_printer_profile(library, printer)
    assert op["id"] == "curaengine5_eryone_thinker_series" and op["name"] == "My Thinker"
    assert op["volume"]["width"] == 250.0 and op["extruder"]["nozzleDiameter"] == 0.6 and op["heatedBed"] is False


def test_catalog_grouped_by_manufacturer(plugin):
    catalog = plugin._get_library().loader.catalog()
    by_id = {m["id"]: m for m in catalog}
    assert by_id["eryone_thinker"]["manufacturer"] == "Eryone"
    assert "ultimaker2" in by_id and len({m["manufacturer"] for m in catalog}) > 10


def test_slice_uses_printer_of_profile(plugin, tmp_path):
    from octoprint_curaengine5 import printers as pm

    library = plugin._get_library()
    printer = pm.apply_machine_settings(library, pm.new_printer(library, "eryone_thinker"), {"machine_width": "180"})
    plugin._save_printers([printer])
    assert pm.select_printer(plugin._printers(), "", {"printer": printer["id"]})["id"] == printer["id"]
    assert pm.select_printer(plugin._printers(), "", None, {"id": "curaengine5_" + printer["id"]})["id"] == printer["id"]
    data = plugin._normalize_profile({"printer": printer["id"], "machine": "eryone_thinker", "quality_type": "normal",
                                      "material": "generic_pla", "user": {"layer_height": "0.3"}})
    path = str(tmp_path / "p.profile")
    plugin.save_slicer_profile(path, octoprint.slicing.SlicingProfile("curaengine5", "p", data))
    assert plugin.get_slicer_profile(path).data["printer"] == printer["id"]
    model = str(tmp_path / "cube.stl")
    stl.write_box(model, 10, 10, 4)
    out = str(tmp_path / "c.gcode")
    ok, result = plugin.do_slice(model, None, machinecode_path=out, profile_path=path)
    assert ok, result
    text = open(out, encoding="utf-8").read()
    assert ";Layer height: 0.3" in text and "PLA" in text.upper()
    defaults, _warn = plugin._profile_defaults(library, printer, "generic_pla", "normal")
    assert defaults["layer_height"] == 0.2 or defaults["layer_height"] > 0


# --- Cura-style settings pane -----------------------------------------------

def test_settings_ui_rows_live_values(plugin):
    from octoprint_curaengine5 import printers as pm, settings_ui as ui

    library, visibility, translations = plugin._ui_support()
    printer = pm.new_printer(library, "eryone_thinker")
    definition = library.loader.load(printer["definition"])
    cats = ui.categories(definition, visibility, "basic", translations.get("fr"))
    assert [c["key"] for c in cats][:2] == ["resolution", "shell"] and cats[0]["label"] != "Quality"  # Cura French translation
    assert len(ui.categories(definition, visibility, "expert", {})) > len(cats) - 1

    body = {"lang": "fr", "quality_type": "normal", "material": "generic_pla"}
    keys = ui.category_keys(definition, "support", visibility, "advanced")
    rows = {r["key"]: r for r in plugin._ui_rows(library, printer, body, keys, True, visibility, translations)}
    assert rows["support_enable"]["label"] != "Generate Support" and rows["support_enable"]["type"] == "bool"
    assert rows["support_structure"]["enabled"] is False
    # live recomputation: enabling supports activates the dependent settings
    body["overrides"] = {"support_enable": "True"}
    live = {r["key"]: r for r in plugin._ui_rows(library, printer, body, keys, False, visibility, translations)}
    assert live["support_enable"]["value"] is True and live["support_structure"]["enabled"] is True
    assert "label" not in live["support_enable"]

    # limits: warning then error on the layer height
    def layer(value):
        b = dict(body, overrides={"layer_height": value})
        return plugin._ui_rows(library, printer, b, [("layer_height", 0)], False, visibility, translations)[0]

    assert layer("0.28")["error"] == "" and layer("0.28")["value"] == 0.28
    assert layer("0.9")["warning"] and not layer("0.9")["error"]
    assert layer("0.0001")["error"]
    assert layer("abc")["error"] == "Invalid value."


def test_profile_overrides_route_per_extruder(plugin):
    from octoprint_curaengine5 import printers as pm, settings_ui as ui

    library = plugin._get_library()
    definition = library.loader.load("eryone_thinker")
    flat, errors = ui.sanitize_overrides(definition, {"layer_height": "0,3", "material_print_temperature": "205",
                                                       "support_enable": True, "bogus": "1", "wall_line_count": "x"})
    assert errors == {"wall_line_count": "invalid"} and flat["layer_height"] == "0.3" and flat["support_enable"] == "True"
    g, e = ui.split_overrides(definition, flat)
    assert "layer_height" in g and "material_print_temperature" in e
    assert ui.flatten_overrides({"user": g, "user_extruders": [e]}) == flat


@pytest.mark.parametrize("printing, paused", [(True, False), (False, True)])
def test_api_slice_refused_while_printing(plugin, printing, paused):
    import flask

    class FakePrinter:
        def is_printing(self):
            return printing

        def is_paused(self):
            return paused

    plugin._printer = FakePrinter()
    app = flask.Flask(__name__)
    with app.test_request_context("/slice", method="POST", json={}):
        # bypass the permission decorator: call the undecorated function
        fn = plugin_module.CuraEngine5Plugin.api_slice
        fn = getattr(fn, "__wrapped__", fn)
        resp = fn(plugin)
    assert resp.status_code == 409
    assert "disabled while printing" in resp.get_data(as_text=True)


def test_software_update_information(plugin):
    info = plugin.get_update_information()["curaengine5"]
    assert info["type"] == "github_release"
    assert (info["user"], info["repo"]) == ("technoo10201", "OctoPrint-CuraEngine5")
    assert info["pip"].endswith("/archive/{target_version}.zip")


def test_pnotify_calls_escape_their_text():
    """PNotify v2 renders text as HTML unless text_escape is set: server messages must stay plain text."""
    import glob
    import os

    base = os.path.join(os.path.dirname(plugin_module.__file__), "static", "js")
    found = 0
    for path in glob.glob(os.path.join(base, "*.js")):
        source = open(path, encoding="utf-8").read()
        for match in re.finditer(r"new PNotify\(\{", source):
            found += 1
            depth, i = 1, match.end()
            while depth and i < len(source):
                depth += {"{": 1, "}": -1}.get(source[i], 0)
                i += 1
            block = source[match.start():i]
            assert "text_escape: true" in block, "{}: PNotify without text_escape".format(os.path.basename(path))
    assert found >= 2


def test_no_html_rendering_in_templates_and_scripts():
    import glob
    import os

    root = os.path.dirname(plugin_module.__file__)
    files = glob.glob(os.path.join(root, "templates", "*.jinja2")) + glob.glob(os.path.join(root, "static", "js", "*.js"))
    for path in files:
        source = open(path, encoding="utf-8").read()
        for sink in ("|safe", "html:", ".html(", "innerHTML", "insertAdjacentHTML", "document.write"):
            assert sink not in source, "{} uses {}".format(os.path.basename(path), sink)
