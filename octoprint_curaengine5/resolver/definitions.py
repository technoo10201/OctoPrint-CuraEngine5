# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loading of Cura definitions (*.def.json) without Uranium.

Reproduces ``UM.Settings.DefinitionContainer``: inheritance by recursive merge
of the dictionaries ("inherits"), application of the "overrides" on the
merged result, then the settings tree ("children").
"""
import ast
import collections
import copy
import json
import logging
import os
import re

from .formula import Formula

_logger = logging.getLogger(__name__)

DEFINITION_VERSION = 2

#: Recognized properties (Uranium + those added by CuraApplication):
#: name -> (is a formula, default value)
PROPERTY_DEFINITIONS = {
    "label": (False, ""),
    "type": (False, ""),
    "icon": (False, ""),
    "unit": (False, ""),
    "description": (False, ""),
    "warning_description": (False, ""),
    "error_description": (False, ""),
    "default_value": (False, 0),
    "value": (True, None),
    "enabled": (True, True),
    "minimum_value": (True, None),
    "maximum_value": (True, None),
    "minimum_value_warning": (True, None),
    "maximum_value_warning": (True, None),
    "options": (False, {}),
    "comments": (False, ""),
    "allow_empty": (True, True),
    "is_uuid": (True, False),
    "regex_blacklist_pattern": (False, ""),
    "warning_value": (True, None),
    "error_value": (True, None),
    "force_depends_on_settings": (False, []),
    # Added by Cura
    "settable_per_mesh": (False, True),
    "settable_per_extruder": (False, True),
    "settable_per_meshgroup": (False, True),
    "settable_globally": (False, True),
    "limit_to_extruder": (True, "-1"),
    "resolve": (True, None),
}

_STRING_PROPERTIES = {
    "label", "type", "icon", "unit", "description", "warning_description",
    "error_description", "comments", "regex_blacklist_pattern",
}


# --- Conversion of text values according to the setting type ----------------

def _to_float(value):
    value = value.replace(",", ".")
    value = re.sub(r"(?<!\.|\w|\d)0+(\d+)", lambda m: m.group(0).lstrip("0"), value)
    try:
        return ast.literal_eval(value)
    except Exception:
        return 0


def _to_int(value):
    try:
        return ast.literal_eval(value)
    except SyntaxError:
        return 0


TYPE_CONVERTERS = {
    "int": _to_int,
    "bool": ast.literal_eval,
    "category": None,
    "str": None,
    "enum": None,
    "float": _to_float,
    "polygon": ast.literal_eval,
    "polygons": ast.literal_eval,
    "vec3": None,
    "extruder": _to_int,
    "optional_extruder": _to_int,
    "[int]": str,
}


def setting_value_from_string(type_name, string_value):
    """Equivalent of ``SettingDefinition.settingValueFromString``.

    :raises ValueError: if the type is unknown (the caller then returns the raw text).
    """
    if type_name not in TYPE_CONVERTERS:
        raise ValueError("Unknown setting type {0}".format(type_name))
    converter = TYPE_CONVERTERS[type_name]
    if converter:
        return converter(string_value)
    return string_value


class SettingDefinition:
    __slots__ = ("key", "parent", "children", "properties")

    def __init__(self, key, parent=None):
        self.key = key
        self.parent = parent
        self.children = []
        self.properties = {}

    def get(self, name):
        if name in self.properties:
            return self.properties[name]
        if name in PROPERTY_DEFINITIONS:
            return PROPERTY_DEFINITIONS[name][1]
        return None

    @property
    def type(self):
        return self.properties.get("type", "")

    def __repr__(self):
        return "<SettingDefinition {}>".format(self.key)


class DefinitionNotFoundError(Exception):
    pass


class Definition:
    """A flattened machine or extruder definition."""

    def __init__(self, definition_id, name, metadata, settings_tree):
        self.id = definition_id
        self.name = name
        self.metadata = metadata
        self.settings = collections.OrderedDict()  # key -> SettingDefinition (first occurrence)
        self.roots = []
        for key, data in settings_tree.items():
            self.roots.append(self._build(key, data, None))

    def _build(self, key, data, parent):
        setting = SettingDefinition(key, parent)
        for prop, value in data.items():
            if prop == "children":
                for child_key, child_data in value.items():
                    setting.children.append(self._build(child_key, child_data, setting))
                continue
            if prop not in PROPERTY_DEFINITIONS:
                _logger.debug("Unknown property %s in setting %s", prop, key)
                continue
            is_function = PROPERTY_DEFINITIONS[prop][0]
            if is_function:
                setting.properties[prop] = Formula(str(value))
            elif prop in _STRING_PROPERTIES:
                setting.properties[prop] = str(value)
            else:
                setting.properties[prop] = value
        self.settings.setdefault(key, setting)
        return setting

    def has(self, key):
        return key in self.settings

    def keys(self):
        return self.settings.keys()

    def get_property(self, key, property_name, context=None):
        """Equivalent of ``DefinitionContainer.getProperty``."""
        setting = self.settings.get(key)
        if setting is None:
            return None
        if property_name not in PROPERTY_DEFINITIONS:
            return None
        value = setting.get(property_name)
        if value is None and property_name == "value":
            value = setting.get("default_value")
        return value

    def has_property(self, key, property_name):
        setting = self.settings.get(key)
        return setting is not None and property_name in PROPERTY_DEFINITIONS

    def setting_type(self, key):
        setting = self.settings.get(key)
        return setting.type if setting is not None else None

    def __repr__(self):
        return "<Definition {}>".format(self.id)


def _merge_dicts(first, second):
    result = copy.deepcopy(first)
    for key, value in second.items():
        if key in result and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _find_in_dict(dictionary, key):
    if key in dictionary:
        return dictionary[key]
    for v in dictionary.values():
        if isinstance(v, dict):
            item = _find_in_dict(v, key)
            if item is not None:
                return item
    return None


class DefinitionLoader:
    """Loads and caches definitions from resource folders."""

    def __init__(self, search_paths):
        self.search_paths = list(search_paths)
        self._cache = {}

    @classmethod
    def from_resources(cls, resources_dir, extra_paths=()):
        return cls(list(extra_paths) + [
            os.path.join(resources_dir, "definitions"),
            os.path.join(resources_dir, "extruders"),
        ])

    def find_file(self, definition_id):
        for directory in self.search_paths:
            path = os.path.join(directory, definition_id + ".def.json")
            if os.path.isfile(path):
                return path
        raise DefinitionNotFoundError(definition_id)

    def _load_json(self, definition_id):
        with open(self.find_file(definition_id), encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=collections.OrderedDict)

    def _resolve_inheritance(self, definition_id, seen=()):
        if definition_id in seen:
            raise ValueError("Circular inheritance: {}".format(" -> ".join(seen + (definition_id,))))
        data = self._load_json(definition_id)
        if "inherits" in data:
            inherited = self._resolve_inheritance(data["inherits"], seen + (definition_id,))
            data = _merge_dicts(inherited, data)
        missing = {"version", "name", "settings", "metadata"} - set(data.keys())
        if missing:
            raise ValueError("Definition {} is incomplete: {}".format(definition_id, ", ".join(sorted(missing))))
        if data["version"] != DEFINITION_VERSION:
            raise ValueError("Definition {} has version {} (expected {})".format(definition_id, data["version"], DEFINITION_VERSION))
        return data

    def load(self, definition_id):
        if definition_id in self._cache:
            return self._cache[definition_id]
        data = self._resolve_inheritance(definition_id)
        for key, value in data.get("overrides", {}).items():
            setting = _find_in_dict(data["settings"], key)
            if setting is None:
                _logger.debug("Cannot override %s in %s", key, definition_id)
            else:
                setting.update(value)
        definition = Definition(definition_id, data["name"], dict(data["metadata"]), data["settings"])
        self._cache[definition_id] = definition
        return definition

    def list_machines(self):
        """List of (id, name, visible, manufacturer) for the available machine definitions."""
        result = []
        for directory in self.search_paths:
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".def.json"):
                    continue
                definition_id = name[: -len(".def.json")]
                try:
                    with open(os.path.join(directory, name), encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    continue
                metadata = data.get("metadata", {})
                if "machine_extruder_trains" not in metadata and definition_id not in ("fdmprinter",):
                    # extruders or abstract definitions without extruders
                    if "position" in metadata:
                        continue
                result.append((definition_id, data.get("name", definition_id), bool(metadata.get("visible", False)),
                               metadata.get("manufacturer", "")))
        return result

    def catalog(self):
        """Visible machines that can be grouped: list of dicts (id, name, manufacturer, visible)."""
        result = []
        for directory in self.search_paths:
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if not name.endswith(".def.json"):
                    continue
                definition_id = name[: -len(".def.json")]
                try:
                    with open(os.path.join(directory, name), encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    continue
                metadata = data.get("metadata", {})
                if "position" in metadata and "machine_extruder_trains" not in metadata:
                    continue
                if not metadata.get("visible", False) and definition_id != "custom":
                    continue
                result.append({
                    "id": definition_id,
                    "name": data.get("name", definition_id),
                    "manufacturer": metadata.get("manufacturer") or "Other",
                })
        return result
