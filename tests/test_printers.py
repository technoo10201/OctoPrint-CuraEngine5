# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from octoprint_curaengine5 import printers as pm
from octoprint_curaengine5.resolver.library import CuraLibrary


@pytest.fixture(scope="module")
def library(resources):
    return CuraLibrary(resources)


def test_new_printer_defaults(library):
    p = pm.new_printer(library, "eryone_thinker", None, existing_ids=["eryone_thinker_series"])
    assert p["name"] == "Eryone Thinker Series"
    assert p["id"] == "eryone_thinker_series_2"
    assert p["printer_profile_id"] == "curaengine5_eryone_thinker_series_2"
    assert p["extruders"][0]["material"] == "eryone_pla"  # preferred material of the definition
    s = pm.machine_settings(library, p)
    assert s["global"]["machine_width"]["value"] == 300
    assert s["extruder"]["machine_nozzle_size"]["value"] == 0.4


def test_apply_machine_settings_keeps_only_changes(library):
    p = pm.new_printer(library, "eryone_thinker", "My Thinker")
    p = pm.apply_machine_settings(library, p, {"machine_width": "300", "machine_height": "380",
                                              "machine_start_gcode": "G28\nM117 Go"},
                                  {"machine_nozzle_size": "0.6"}, material="generic_abs_175", name="Thinker V2")
    assert p["name"] == "Thinker V2"
    assert p["definition_changes"] == {"machine_height": "380", "machine_start_gcode": "G28\nM117 Go"}
    assert p["extruders"][0]["definition_changes"] == {"machine_nozzle_size": "0.6"}
    assert p["extruders"][0]["material"] == "generic_abs_175"
    # going back to the definition value removes the change
    p = pm.apply_machine_settings(library, p, {"machine_height": "400"})
    assert "machine_height" not in p["definition_changes"]


def test_octoprint_printer_profile(library):
    p = pm.new_printer(library, "eryone_thinker", "Thinker")
    p = pm.apply_machine_settings(library, p, {}, {"machine_nozzle_size": "0.6"})
    op = pm.octoprint_printer_profile(library, p)
    assert op["id"] == "curaengine5_thinker" and op["name"] == "Thinker"
    assert op["volume"] == {"width": 300.0, "depth": 300.0, "height": 400.0,
                            "formFactor": "rectangular", "origin": "lowerleft"}
    assert op["heatedBed"] is True and op["extruder"]["nozzleDiameter"] == 0.6


def test_select_printer():
    a = {"id": "a", "printer_profile_id": "curaengine5_a"}
    b = {"id": "b", "printer_profile_id": "curaengine5_b"}
    assert pm.select_printer([a, b], "a", {"printer": "b"}) is b
    assert pm.select_printer([a, b], "a", {}, {"id": "curaengine5_b"}) is b
    assert pm.select_printer([a, b], "b", {}, {"id": "_default"}) is b
    assert pm.select_printer([a, b], None) is a
    assert pm.select_printer([], None) is None


def test_migrate_legacy():
    p = pm.migrate_legacy_machine({"definition": "eryone_thinker", "definition_changes": {"machine_height": "380"}})
    assert p["definition"] == "eryone_thinker" and p["definition_changes"] == {"machine_height": "380"}
