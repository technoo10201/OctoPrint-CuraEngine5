# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import os

import pytest

from octoprint_curaengine5.resolver import DefinitionLoader, InstanceContainer, Machine
from octoprint_curaengine5.resolver.materials import parse_material

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
REFERENCE = os.path.join(HERE, "reference")

_DEFAULT_APPDIR = "cura-appdir"


def cura_appdir():
    return os.environ.get("CURAENGINE5_CURA_APPDIR", _DEFAULT_APPDIR)


def cura_resources():
    return os.environ.get(
        "CURAENGINE5_RESOURCES", os.path.join(cura_appdir(), "share", "cura", "resources")
    )


@pytest.fixture(scope="session")
def resources():
    path = cura_resources()
    if not os.path.isdir(os.path.join(path, "definitions")):
        pytest.skip("Cura 5.11 resources not found (CURAENGINE5_RESOURCES)")
    return path


@pytest.fixture(scope="session")
def loader(resources):
    return DefinitionLoader.from_resources(resources)


def load_scenario(path, loader, resources):
    with open(path, encoding="utf-8") as f:
        scenario = json.load(f)
    base = os.path.dirname(path)
    machine_def = loader.load(scenario["machine"])

    def container(slot, spec, owner):
        if isinstance(spec, dict) and "xml" in spec:
            path = spec["xml"]
            path = os.path.join(resources, path[4:]) if path.startswith("res:") else os.path.join(base, path)
            base_file = os.path.basename(path).split(".")[0]
            with open(path, encoding="utf-8") as f:
                material = parse_material(f.read(), base_file)
            return InstanceContainer(base_file, "material", machine_def,
                                     values=material.values_for(scenario["machine"]), metadata=material.metadata)
        if isinstance(spec, str):
            if spec.startswith("res:"):
                file_path = os.path.join(resources, spec[4:])
            else:
                file_path = os.path.join(base, spec)
            with open(file_path, encoding="utf-8") as f:
                ic = InstanceContainer.from_cfg(f.read(), machine_def, container_id=owner + "_" + slot)
            ic.type = slot
            return ic
        return InstanceContainer(owner + "_" + slot, slot, machine_def, values=spec)

    global_containers = {s: container(s, v, "global") for s, v in scenario.get("global", {}).items()}
    extruders = []
    for i, ext in enumerate(scenario.get("extruders", [])):
        extruders.append({s: container(s, v, "ext%d" % i) for s, v in ext.get("containers", {}).items()})
    return Machine(loader, scenario["machine"], global_containers, extruders)
