# SPDX-License-Identifier: AGPL-3.0-or-later
"""Building a machine through CuraLibrary (the path used by the plugin)
must give exactly Uranium's values for a real configuration with a custom quality-changes profile."""
import json
import os

import pytest

from octoprint_curaengine5.resolver.containers import parse_cfg
from octoprint_curaengine5.resolver.library import CuraLibrary

from .conftest import DATA, REFERENCE
from .test_resolver_reference import _jsonable, differences


# Fixture files for the custom quality-changes profile (to be renamed by the coordinator).
GLOBAL_QC_FIXTURE = "custom_pla_global_qc.inst.cfg"
EXTRUDER_QC_FIXTURE = "custom_pla_extruder_qc.inst.cfg"
REFERENCE_FIXTURE = "eryone_thinker_custom.reference.json"


def _values(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return parse_cfg(f.read())[2]


@pytest.fixture(scope="module")
def library(resources):
    return CuraLibrary(resources)


def test_build_machine_matches_uranium(library):
    machine_config = {
        "definition": "eryone_thinker",
        "definition_changes": _values("eryone_thinker_definition_changes.inst.cfg"),
        "extruders": [{"material": "generic_abs"}],
    }
    profile = {
        "quality_type": "normal",
        "global": _values(GLOBAL_QC_FIXTURE),
        "extruders": [_values(EXTRUDER_QC_FIXTURE)],
    }
    overrides = {"layer_height_0": "0.2", "extruders": [{"initial_layer_line_width_factor": "110"}]}
    machine = library.build_machine(machine_config, profile, overrides)
    assert machine.warnings == []
    ours = _jsonable(machine.resolve_all())
    with open(os.path.join(REFERENCE, REFERENCE_FIXTURE), encoding="utf-8") as f:
        reference = json.load(f)
    diffs = differences(ours["global"], reference["global"]) + differences(ours["extruders"][0], reference["extruders"][0])
    assert not diffs, diffs


def test_quality_types(library):
    types = library.quality_types("eryone_thinker")
    assert {"normal", "fine", "high", "fast", "verydraft"} <= set(types)


def test_unknown_material_warns(library):
    machine = library.build_machine({"definition": "eryone_thinker", "extruders": [{"material": "nope"}]})
    assert any("nope" in w for w in machine.warnings)


def test_default_material_from_definition(library):
    machine = library.build_machine({"definition": "eryone_thinker"})
    assert machine.get("material_type", 0) == "PLA"  # preferred_material = eryone_pla
