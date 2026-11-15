# SPDX-License-Identifier: AGPL-3.0-or-later
"""Printers configured in the plugin (the equivalent of Cura's "machines").

A printer = a Cura definition + the modified machine settings
("definition_changes", like Cura's "Machine Settings" dialog) +
default material, and the OctoPrint printer profile linked to it.
"""
import copy
import re

#: Editable (global) machine settings, as in Cura.
MACHINE_KEYS = [
    "machine_width", "machine_depth", "machine_height", "machine_shape",
    "machine_center_is_zero", "machine_heated_bed", "machine_gcode_flavor",
    "machine_start_gcode", "machine_end_gcode",
]
#: Settings editable per extruder.
EXTRUDER_KEYS = ["machine_nozzle_size", "material_diameter", "machine_nozzle_offset_x", "machine_nozzle_offset_y"]

PRINTER_PROFILE_PREFIX = "curaengine5_"


def slugify(name):
    slug = re.sub(r"[^a-z0-9_]+", "_", (name or "").lower()).strip("_")
    return slug or "printer"


def unique_id(base, existing):
    candidate, n = base, 2
    while candidate in existing:
        candidate = "{}_{}".format(base, n)
        n += 1
    return candidate


def _as_text(value):
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def new_printer(library, definition_id, name=None, existing_ids=()):
    definition = library.loader.load(definition_id)
    name = (name or definition.name).strip()
    printer_id = unique_id(slugify(name), set(existing_ids))
    machine = library.build_machine({"definition": definition_id})
    material = ""
    if str(definition.metadata.get("has_materials", "")).lower() == "true":
        material = library.resolve_material_id(definition, library.default_material(definition),
                                               machine.get("material_diameter", 0)) or ""
    return {
        "id": printer_id,
        "name": name,
        "definition": definition_id,
        "definition_changes": {},
        "extruders": [{"material": material, "variant": "", "definition_changes": {}}],
        "printer_profile_id": PRINTER_PROFILE_PREFIX + printer_id,
    }


def machine_config(printer):
    return {
        "definition": printer["definition"],
        "definition_changes": dict(printer.get("definition_changes") or {}),
        "extruders": copy.deepcopy(printer.get("extruders") or [{}]),
    }


def machine_settings(library, printer):
    """Effective values and definition defaults of the machine settings."""
    current = library.build_machine(machine_config(printer))
    base = library.build_machine({"definition": printer["definition"]})
    result = {"global": {}, "extruder": {}}
    for key in MACHINE_KEYS:
        result["global"][key] = {"value": current.get(key), "default": base.get(key)}
    for key in EXTRUDER_KEYS:
        result["extruder"][key] = {"value": current.get(key, 0), "default": base.get(key, 0)}
    result["extruder_count"] = current.get("machine_extruder_count")
    return result


def apply_machine_settings(library, printer, values, extruder_values=None, material=None, name=None):
    """Update the printer: only values that differ from the definition are kept."""
    printer = copy.deepcopy(printer)
    if name:
        printer["name"] = name.strip()
    base = library.build_machine({"definition": printer["definition"]})
    changes = dict(printer.get("definition_changes") or {})
    for key, value in (values or {}).items():
        if key not in MACHINE_KEYS:
            continue
        if value is None or str(value).strip() == "" or _same(value, base.get(key)):
            changes.pop(key, None)
        else:
            changes[key] = _as_text(value)
    printer["definition_changes"] = changes
    ext = printer["extruders"][0] if printer.get("extruders") else {}
    ext_changes = dict(ext.get("definition_changes") or {})
    for key, value in (extruder_values or {}).items():
        if key not in EXTRUDER_KEYS:
            continue
        if value is None or str(value).strip() == "" or _same(value, base.get(key, 0)):
            ext_changes.pop(key, None)
        else:
            ext_changes[key] = _as_text(value)
    ext["definition_changes"] = ext_changes
    if material is not None:
        ext["material"] = material
    ext.setdefault("variant", "")
    printer["extruders"] = [ext] + list(printer.get("extruders") or [])[1:]
    return printer


def _same(value, default):
    if isinstance(default, bool):
        return _as_text(value).lower() == str(default).lower()
    if isinstance(default, (int, float)):
        try:
            return abs(float(value) - float(default)) < 1e-9
        except (TypeError, ValueError):
            return False
    return str(value).replace("\r\n", "\n") == str(default)


def octoprint_printer_profile(library, printer):
    """Matching OctoPrint printer profile (volume, build plate, nozzle)."""
    machine = library.build_machine(machine_config(printer))
    definition = machine.definition
    shape = machine.get("machine_shape")
    return {
        "id": printer["printer_profile_id"],
        "name": printer["name"],
        "model": definition.name,
        "volume": {
            "width": float(machine.get("machine_width")),
            "depth": float(machine.get("machine_depth")),
            "height": float(machine.get("machine_height")),
            "formFactor": "circular" if shape == "elliptic" else "rectangular",
            "origin": "center" if machine.get("machine_center_is_zero") else "lowerleft",
        },
        "heatedBed": bool(machine.get("machine_heated_bed")),
        "extruder": {
            "count": int(machine.get("machine_extruder_count") or 1),
            "nozzleDiameter": float(machine.get("machine_nozzle_size", 0)),
        },
    }


def select_printer(printers, default_id=None, profile=None, printer_profile=None):
    """Pick the printer for a slicing job: the one of the print profile, otherwise
    the one linked to the chosen OctoPrint printer profile, otherwise the default one."""
    by_id = {p["id"]: p for p in printers}
    if profile and profile.get("printer") in by_id:
        return by_id[profile["printer"]]
    if printer_profile:
        for p in printers:
            if p.get("printer_profile_id") == printer_profile.get("id"):
                return p
    if default_id in by_id:
        return by_id[default_id]
    return printers[0] if printers else None


def migrate_legacy_machine(machine):
    """Old single "machine" setting -> first printer."""
    if not machine or not machine.get("definition"):
        return None
    return {
        "id": "printer",
        "name": "Printer",
        "definition": machine["definition"],
        "definition_changes": dict(machine.get("definition_changes") or {}),
        "extruders": copy.deepcopy(machine.get("extruders") or [{"material": "", "variant": "", "definition_changes": {}}]),
        "printer_profile_id": PRINTER_PROFILE_PREFIX + "printer",
    }


#: Settings offered in the simple profile page (Cura key, label, field type).
SIMPLE_SETTINGS = [
    ("layer_height", "Layer height (mm)", "number"),
    ("wall_line_count", "Wall line count", "number"),
    ("infill_sparse_density", "Infill density (%)", "number"),
    ("material_print_temperature", "Printing temperature (°C)", "number"),
    ("material_bed_temperature", "Build plate temperature (°C)", "number"),
    ("speed_print", "Print speed (mm/s)", "number"),
    ("support_enable", "Generate supports", "bool"),
    ("adhesion_type", "Build plate adhesion", "select:skirt,brim,raft,none"),
]
