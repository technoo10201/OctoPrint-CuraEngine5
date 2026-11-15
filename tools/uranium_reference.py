#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Computes the reference values of a scenario with the real Uranium/Cura code.

Development tool only: it imports the Python code of Uranium and Cura
(LGPL-3.0) from an extracted UltiMaker Cura AppImage, without copying it.
It is not used by the plugin.

Requirements: Python 3.12 with PyQt6, cryptography and numpy<2 (see tools/README).

Usage:
    python uranium_reference.py --appdir <squashfs-root> [--resources <dir>] -o reference.json scenario.json
"""
import argparse
import collections
import configparser
import enum
import json
import os
import sys
import types


def _install_stubs(appdir):
    sys.path.append(appdir)

    class _MachineManager:
        def __init__(self):
            self.activeMachine = None
            self.defaultExtruderPosition = "0"

    class _ExtruderManager:
        def __init__(self, app):
            self._app = app

        def getActiveExtruderStacks(self):
            stack = self._app.getGlobalContainerStack()
            return stack.extruderList if stack else []

    class CuraApplication:
        SettingVersion = 25
        _instance = None

        def __init__(self):
            self._machine_manager = _MachineManager()
            self._extruder_manager = _ExtruderManager(self)

        @classmethod
        def getInstance(cls):
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

        def getMachineManager(self):
            return self._machine_manager

        def getExtruderManager(self):
            return self._extruder_manager

        def getGlobalContainerStack(self):
            return self._machine_manager.activeMachine

    um_app = types.ModuleType("UM.Application")

    class Application:
        @staticmethod
        def getInstance():
            return CuraApplication.getInstance()

    um_app.Application = Application
    sys.modules["UM.Application"] = um_app

    mod = types.ModuleType("cura.CuraApplication")
    mod.CuraApplication = CuraApplication
    sys.modules["cura.CuraApplication"] = mod
    import cura  # the real package (its __init__ is empty)
    cura.CuraApplication = mod

    class ConnectionType(enum.IntEnum):
        NotConnected = 0
        UsbConnection = 1
        NetworkConnection = 2
        CloudConnection = 3

    pkg = types.ModuleType("cura.PrinterOutput")
    pkg.__path__ = [os.path.join(appdir, "cura", "PrinterOutput")]
    sys.modules["cura.PrinterOutput"] = pkg
    cura.PrinterOutput = pkg
    pod = types.ModuleType("cura.PrinterOutput.PrinterOutputDevice")
    pod.ConnectionType = ConnectionType
    sys.modules["cura.PrinterOutput.PrinterOutputDevice"] = pod
    return CuraApplication


def _read_cfg_values(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    with open(path, encoding="utf-8") as f:
        parser.read_file(f)
    return dict(parser["values"]) if parser.has_section("values") else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--appdir", required=True)
    ap.add_argument("--resources", help="share/cura/resources folder (default: <appdir>/share/cura/resources)")
    ap.add_argument("-o", "--output", required=True, help="output JSON file")
    ap.add_argument("scenario")
    args = ap.parse_args()

    with open(args.scenario, encoding="utf-8") as f:
        scenario = json.load(f)
    base = os.path.dirname(os.path.abspath(args.scenario))

    CuraApplication = _install_stubs(args.appdir)

    from UM.Settings.SettingDefinition import SettingDefinition, DefinitionPropertyType, toIntConversion
    from UM.Settings.Validator import Validator
    from UM.Settings.DefinitionContainer import DefinitionContainer
    from UM.Settings.InstanceContainer import InstanceContainer
    from UM.Settings.SettingFunction import SettingFunction

    SettingDefinition.addSupportedProperty("settable_per_mesh", DefinitionPropertyType.Any, default=True, read_only=True)
    SettingDefinition.addSupportedProperty("settable_per_extruder", DefinitionPropertyType.Any, default=True, read_only=True)
    SettingDefinition.addSupportedProperty("settable_per_meshgroup", DefinitionPropertyType.Any, default=True, read_only=True)
    SettingDefinition.addSupportedProperty("settable_globally", DefinitionPropertyType.Any, default=True, read_only=True)
    SettingDefinition.addSupportedProperty("limit_to_extruder", DefinitionPropertyType.Function, default="-1", depends_on="value")
    SettingDefinition.addSupportedProperty("resolve", DefinitionPropertyType.Function, default=None, depends_on="value")
    SettingDefinition.addSettingType("extruder", None, toIntConversion, Validator)
    SettingDefinition.addSettingType("optional_extruder", None, toIntConversion, None)
    SettingDefinition.addSettingType("[int]", None, str, None)

    resources = args.resources or os.path.join(args.appdir, "share", "cura", "resources")
    search = [os.path.join(resources, "definitions"), os.path.join(resources, "extruders")]

    def _load_file(self, file_name):
        for d in search:
            path = os.path.join(d, file_name + ".def.json")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh, object_pairs_hook=collections.OrderedDict)
        raise FileNotFoundError(file_name)

    DefinitionContainer._loadFile = _load_file
    # No version upgrade: the resource files are already up to date.
    DefinitionContainer._updateSerialized = classmethod(lambda cls, serialized, file_name=None: serialized)
    DefinitionContainer._trustHook = lambda self, file_name: True

    def load_definition(def_id):
        dc = DefinitionContainer(def_id)
        with open(_find_def(def_id), encoding="utf-8") as fh:
            dc.deserialize(fh.read(), def_id + ".def.json")
        return dc

    def _find_def(def_id):
        for d in search:
            path = os.path.join(d, def_id + ".def.json")
            if os.path.exists(path):
                return path
        raise FileNotFoundError(def_id)

    def make_instance(name, slot_type, spec, definition):
        if isinstance(spec, dict) and "xml" in spec:
            return make_material(name, spec["xml"])
        ic = InstanceContainer(name)
        ic.setMetaDataEntry("type", slot_type)
        ic._definition = definition
        if isinstance(spec, str):
            if spec.startswith("res:"):
                path = os.path.join(resources, spec[4:])
            else:
                path = spec if os.path.isabs(spec) else os.path.join(base, spec)
            values = _read_cfg_values(path)
        else:
            values = spec
        for k, v in values.items():
            ic.setProperty(k, "value", v)
        return ic

    def make_material(name, spec):
        sys.path.append(os.path.join(args.appdir, "share", "cura", "plugins"))
        from UM.Settings.ContainerRegistry import ContainerRegistry

        class _Registry:
            def findDefinitionContainersMetadata(self, **kwargs):
                return []

            def isLoaded(self, container_id):
                return False

            def isReadOnly(self, container_id):
                return True

            def __getattr__(self, item):
                return lambda *a, **k: None

        ContainerRegistry.getInstance = classmethod(lambda cls: _Registry())
        from cura.PrinterOutput.FormatMaps import FormatMaps
        FormatMaps.getProductIdMap = classmethod(lambda cls: {})  # no product -> id table (UltiMaker machines)
        from XmlMaterialProfile.XmlMaterialProfile import XmlMaterialProfile
        XmlMaterialProfile._updateSerialized = classmethod(lambda cls, serialized, file_name=None: serialized)
        XmlMaterialProfile._trustHook = lambda self, file_name: True
        path = os.path.join(resources, spec[4:]) if spec.startswith("res:") else os.path.join(base, spec)
        base_file = os.path.basename(path).split(".")[0]
        material = XmlMaterialProfile(base_file)
        material._definition = machine_def
        with open(path, encoding="utf-8") as fh:
            material.deserialize(fh.read(), path)
        material._definition = machine_def
        material.setMetaDataEntry("type", "material")
        return material

    from cura.Settings.GlobalStack import GlobalStack
    from cura.Settings.ExtruderStack import ExtruderStack
    from cura.Settings.CuraFormulaFunctions import CuraFormulaFunctions

    app = CuraApplication.getInstance()
    funcs = CuraFormulaFunctions(app)
    SettingFunction.registerOperator("extruderValue", funcs.getValueInExtruder)
    SettingFunction.registerOperator("extruderValues", funcs.getValuesInAllExtruders)
    SettingFunction.registerOperator("anyExtruderWithMaterial", funcs.getExtruderPositionWithMaterial)
    SettingFunction.registerOperator("anyExtruderNrWithOrDefault", funcs.getAnyExtruderPositionWithOrDefault)
    SettingFunction.registerOperator("resolveOrValue", funcs.getResolveOrValue)
    SettingFunction.registerOperator("defaultExtruderPosition", funcs.getDefaultExtruderPosition)
    SettingFunction.registerOperator("valueFromContainer", funcs.getValueFromContainerAtIndex)
    SettingFunction.registerOperator("extruderValueFromContainer", funcs.getValueFromContainerAtIndexInExtruder)

    setters = {
        "user": "setUserChanges",
        "quality_changes": "setQualityChanges",
        "intent": "setIntent",
        "quality": "setQuality",
        "material": "setMaterial",
        "variant": "setVariant",
        "definition_changes": "setDefinitionChanges",
    }

    machine_def = load_definition(scenario["machine"])
    global_stack = GlobalStack("global")
    global_stack.setDefinition(machine_def)
    for slot, spec in scenario.get("global", {}).items():
        getattr(global_stack, setters[slot])(make_instance("global_" + slot, slot, spec, machine_def))
    app.getMachineManager().activeMachine = global_stack

    extruder_stacks = []
    for position, ext in enumerate(scenario.get("extruders", [])):
        ext_def = load_definition(ext["definition"])
        stack = ExtruderStack("extruder_%d" % position)
        stack.setMetaDataEntry("position", str(position))
        stack.setDefinition(ext_def)
        for slot, spec in ext.get("containers", {}).items():
            getattr(stack, setters[slot])(make_instance("ext%d_%s" % (position, slot), slot, spec, machine_def))
        stack.setNextStack(global_stack)  # also calls global_stack.addExtruder()
        extruder_stacks.append(stack)

    def dump(stack):
        out = {}
        for key in sorted(stack.getAllKeys()):
            if stack.getProperty(key, "type") == "category":
                continue
            out[key] = _jsonable(stack.getProperty(key, "value"))
        return out

    result = {
        "global": dump(global_stack),
        "extruders": [dump(s) for s in extruder_stacks],
    }
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def _jsonable(value):
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


if __name__ == "__main__":
    main()
