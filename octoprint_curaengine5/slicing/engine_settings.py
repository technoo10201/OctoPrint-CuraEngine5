# SPDX-License-Identifier: AGPL-3.0-or-later
"""Construction of the settings sent to CuraEngine (port of Cura 5.11's StartSliceJob).

On the command line, CuraEngine does not evaluate the definitions' formulas:
so all values are passed already resolved, as ``str(value)``,
exactly as Cura does in its protobuf message.
"""
import re
import time

from . import gcode_tokens

_BED_TEMPERATURE_SETTINGS = ["material_bed_temperature", "material_bed_temperature_layer_0"]
_PRINT_TEMPERATURE_SETTINGS = [
    "material_print_temperature", "material_print_temperature_layer_0",
    "default_material_print_temperature", "material_initial_print_temperature",
    "material_final_print_temperature", "material_standby_temperature", "print_temperature",
]


def initial_extruder_nr(machine):
    """First extruder used (ExtruderManager.getInitialExtruderNr)."""
    stack = machine.global_stack

    def first_used():
        for extruder in stack.extruder_list:
            if extruder.enabled:
                return extruder.get_property("extruder_nr", "value")
        return 0

    adhesion_type = stack.get_property("adhesion_type", "value")
    if adhesion_type in {"skirt", "brim"}:
        nr = stack.get_property("skirt_brim_extruder_nr", "value")
        return first_used() if nr == -1 else nr
    if adhesion_type == "raft":
        return stack.get_property("raft_base_extruder_nr", "value")
    if (stack.get_property("support_enable", "value") or stack.get_property("support_structure", "value") == "tree") \
            and stack.get_property("support_brim_enable", "value"):
        return stack.get_property("support_infill_extruder_nr", "value")
    return first_used()


def _replacement_tokens(machine, stack, now):
    result = {}
    for key in stack.all_keys():
        if stack.get_property(key, "type") == "category":
            continue
        result[key] = stack.get_property(key, "value")
    material = stack.slots["material"]
    result["material_id"] = material.metadata.get("base_file", "")
    result["material_type"] = material.metadata.get("material", "")
    result["material_name"] = material.metadata.get("name", "empty" if material.is_empty() else material.name)
    result["material_brand"] = material.metadata.get("brand", "")
    quality = stack.slots["quality"]
    result["quality_name"] = "Not supported" if quality.id.startswith("empty") else quality.name
    quality_changes = stack.slots["quality_changes"]
    result["quality_changes_name"] = quality_changes.metadata.get("name", quality_changes.name)
    result["print_bed_temperature"] = result["material_bed_temperature"]
    result["print_temperature"] = result["material_print_temperature"]
    result["travel_speed"] = result["speed_travel"]
    result["time"] = time.strftime("%H:%M:%S", now)
    result["date"] = time.strftime("%d-%m-%Y", now)
    result["day"] = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][int(time.strftime("%w", now))]
    result["initial_extruder_nr"] = initial_extruder_nr(machine)
    return result


def build_engine_settings(machine, now=None):
    """Returns (global settings, [per-extruder settings]) as text, ready for ``-s key=value``."""
    now = now or time.localtime()
    all_settings = {"-1": _replacement_tokens(machine, machine.global_stack, now)}
    for extruder in machine.global_stack.extruder_list:
        nr = extruder.get_property("extruder_nr", "value")
        all_settings[str(nr)] = _replacement_tokens(machine, extruder, now)

    # --- global (_buildGlobalSettingsMessage) ---
    settings = dict(all_settings["-1"])
    start_gcode = re.sub(r";.+?(\n|$)", "\n", settings["machine_start_gcode"])
    if settings["material_bed_temp_prepend"]:
        pattern = r"\{(%s)(,\s?\w+)?\}" % "|".join(_BED_TEMPERATURE_SETTINGS)
        settings["material_bed_temp_prepend"] = re.search(pattern, start_gcode) is None
    if settings["material_print_temp_prepend"]:
        pattern = r"\{(%s)(,\s?\w+)?\}" % "|".join(_PRINT_TEMPERATURE_SETTINGS)
        settings["material_print_temp_prepend"] = re.search(pattern, start_gcode) is None
    first = initial_extruder_nr(machine)
    settings["machine_start_gcode"] = gcode_tokens.expand(machine, all_settings, settings["machine_start_gcode"], first)
    settings["machine_end_gcode"] = gcode_tokens.expand(machine, all_settings, settings["machine_end_gcode"], first)
    settings["nozzle_offsetting_for_disallowed_areas"] = machine.definition.metadata.get("nozzle_offsetting_for_disallowed_areas", True)
    global_settings = {key: str(value) for key, value in settings.items()}

    # --- extruders (_buildExtruderMessage) ---
    global_definition = machine.definition
    extruder_settings = []
    for extruder in machine.global_stack.extruder_list:
        nr = extruder.get_property("extruder_nr", "value")
        settings = dict(all_settings[str(nr)])
        settings["material_guid"] = extruder.slots["material"].metadata.get("GUID", "")
        for key in ("machine_extruder_prestart_code", "machine_extruder_start_code", "machine_extruder_end_code"):
            settings[key] = gcode_tokens.expand(machine, all_settings, settings[key], nr)
        own_definition = extruder.definition
        result = {}
        for key, value in settings.items():
            if not global_definition.get_property(key, "settable_per_extruder") and \
                    not own_definition.get_property(key, "settable_per_extruder"):
                continue
            result[key] = str(value)
        extruder_settings.append(result)
    return global_settings, extruder_settings
