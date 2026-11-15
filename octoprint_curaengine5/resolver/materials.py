# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading of Cura materials (*.xml.fdm_material), based on XmlMaterialProfile.

Supports the common settings and the <machine> sections whose
product identifier matches the machine definition. Settings
specific to a nozzle (<hotend>) are only applied if the nozzle name is
provided.
"""
import os
import xml.etree.ElementTree as ET

NS = {"um": "http://www.ultimaker.com/material", "cura": "http://www.ultimaker.com/cura"}

SETTINGS_MAP = {
    "print temperature": "default_material_print_temperature",
    "heated bed temperature": "default_material_bed_temperature",
    "standby temperature": "material_standby_temperature",
    "processing temperature graph": "material_flow_temp_graph",
    "print cooling": "cool_fan_speed",
    "retraction amount": "retraction_amount",
    "retraction speed": "retraction_speed",
    "adhesion tendency": "material_adhesion_tendency",
    "surface energy": "material_surface_energy",
    "build volume temperature": "build_volume_temperature",
    "anti ooze retract position": "material_anti_ooze_retracted_position",
    "anti ooze retract speed": "material_anti_ooze_retraction_speed",
    "break preparation position": "material_break_preparation_retracted_position",
    "break preparation speed": "material_break_preparation_speed",
    "break preparation temperature": "material_break_preparation_temperature",
    "break position": "material_break_retracted_position",
    "flush purge speed": "material_flush_purge_speed",
    "flush purge length": "material_flush_purge_length",
    "end of filament purge speed": "material_end_of_filament_purge_speed",
    "end of filament purge length": "material_end_of_filament_purge_length",
    "maximum park duration": "material_maximum_park_duration",
    "no load move factor": "material_no_load_move_factor",
    "break speed": "material_break_speed",
    "break temperature": "material_break_temperature",
}
PROPERTIES_MAP = {"diameter": "material_diameter"}
METADATA_MAP = {"GUID": "material_guid", "material": "material_type", "brand": "material_brand"}


def _tag(element):
    return element.tag.split("}", 1)[-1]


def _um_settings(node):
    values = {}
    for entry in node.iterfind("./um:setting", NS):
        key = entry.get("key")
        if key not in SETTINGS_MAP:
            continue
        if key == "processing temperature graph":
            points = [[float(p.get("flow")), float(p.get("temperature"))] for p in entry.iterfind("./um:point", NS)]
            values[SETTINGS_MAP[key]] = str(points)
        else:
            values[SETTINGS_MAP[key]] = entry.text
    return values


def _cura_settings(node):
    values = {}
    for entry in node.iterfind("./cura:setting", NS):
        value = entry.text or ""
        if value.lower() == "yes":
            value = True
        elif value.lower() == "no":
            value = False
        values[entry.get("key")] = value
    return values


class Material:
    def __init__(self, base_file, metadata, common_values, machines):
        self.id = base_file
        self.base_file = base_file
        self.metadata = metadata
        self.common_values = common_values
        self.machines = machines  # list of (product identifiers, values, {nozzle: values})

    @property
    def name(self):
        return self.metadata.get("name", self.id)

    @property
    def approximate_diameter(self):
        return self.metadata.get("approximate_diameter")

    def values_for(self, machine_id=None, hotend=None):
        """Material setting values for a machine (and a nozzle)."""
        values = dict(self.common_values)
        for products, machine_values, hotends in self.machines:
            if machine_id in products:
                values.update(machine_values)
                if hotend and hotend in hotends:
                    values.update(hotends[hotend])
                break
        return values


def parse_material(xml_text, base_file):
    root = ET.fromstring(xml_text)
    metadata = {"base_file": base_file}
    common = {}
    meta_node = root.find("./um:metadata", NS)
    if meta_node is not None:
        for entry in meta_node.iterfind("./*", NS):
            tag = _tag(entry)
            if tag == "name":
                for sub in entry:
                    sub_tag = _tag(sub)
                    metadata[sub_tag] = sub.text or ""
                    if sub_tag in METADATA_MAP:
                        common[METADATA_MAP[sub_tag]] = sub.text or ""
                continue
            metadata[tag] = entry.text or ""
            if tag in METADATA_MAP:
                common[METADATA_MAP[tag]] = entry.text or ""
    # Displayed name as in Cura: "<color> <material>", otherwise the material
    color = metadata.get("color", "")
    metadata["name"] = metadata.get("material", base_file) if color in ("", "Generic") else "{} {}".format(color, metadata.get("material", ""))
    properties = {}
    props_node = root.find("./um:properties", NS)
    if props_node is not None:
        for entry in props_node:
            tag = _tag(entry)
            properties[tag] = entry.text
            if tag in PROPERTIES_MAP:
                common[PROPERTIES_MAP[tag]] = entry.text
    metadata["properties"] = properties
    try:
        metadata["approximate_diameter"] = str(round(float(properties.get("diameter", 2.85))))
    except ValueError:
        metadata["approximate_diameter"] = "3"
    settings_node = root.find("./um:settings", NS)
    machines = []
    if settings_node is not None:
        common.update(_um_settings(settings_node))
        common.update(_cura_settings(settings_node))
        for machine in settings_node.iterfind("./um:machine", NS):
            values = _um_settings(machine)
            values.update(_cura_settings(machine))
            products = {i.get("product") for i in machine.iterfind("./um:machine_identifier", NS)}
            hotends = {}
            for hotend in machine.iterfind("./um:hotend", NS):
                if hotend.get("id"):
                    hv = _um_settings(hotend)
                    hv.update(_cura_settings(hotend))
                    hotends[hotend.get("id")] = hv
            machines.append((products, values, hotends))
    return Material(base_file, metadata, common, machines)


class MaterialLibrary:
    def __init__(self, directories):
        self.directories = [d for d in directories if os.path.isdir(d)]
        self._index = None

    def _scan(self):
        index = {}
        for directory in self.directories:
            for dirpath, _, filenames in os.walk(directory):
                for name in filenames:
                    if name.endswith(".xml.fdm_material"):
                        index.setdefault(name[: -len(".xml.fdm_material")], os.path.join(dirpath, name))
        return index

    @property
    def index(self):
        if self._index is None:
            self._index = self._scan()
        return self._index

    def load(self, material_id):
        path = self.index.get(material_id)
        if path is None:
            raise KeyError("Material not found: {}".format(material_id))
        with open(path, encoding="utf-8") as f:
            return parse_material(f.read(), material_id)

    def list(self, diameter=None):
        result = []
        for material_id in sorted(self.index):
            try:
                material = self.load(material_id)
            except (ET.ParseError, OSError):
                continue
            if diameter is not None and material.approximate_diameter != str(round(float(diameter))):
                continue
            result.append(material)
        return result
