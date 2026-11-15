# SPDX-License-Identifier: AGPL-3.0-or-later
"""A complete slicing run, independent of OctoPrint (testable on its own)."""
import logging
import os
import shutil
import tempfile
import time

from . import engine, stl
from .engine_settings import build_engine_settings

_logger = logging.getLogger(__name__)

#: "Generic" definitions for which the OctoPrint printer profile
#: provides the dimensions and the nozzle ("auto" mode).
GENERIC_DEFINITIONS = {"custom", "fdmprinter"}


def printer_profile_settings(printer_profile):
    """Translates an OctoPrint printer profile into Cura machine settings."""
    if not printer_profile:
        return {}, []
    volume = printer_profile.get("volume", {}) or {}
    extruder = printer_profile.get("extruder", {}) or {}
    result = {}
    for key, cura_key in (("width", "machine_width"), ("depth", "machine_depth"), ("height", "machine_height")):
        if volume.get(key):
            result[cura_key] = str(volume[key])
    if "origin" in volume:
        result["machine_center_is_zero"] = str(volume.get("origin") == "center")
    if volume.get("formFactor"):
        result["machine_shape"] = "elliptic" if volume["formFactor"] == "circular" else "rectangular"
    if "heatedBed" in printer_profile:
        result["machine_heated_bed"] = str(bool(printer_profile["heatedBed"]))
    if extruder.get("count"):
        result["machine_extruder_count"] = str(int(extruder["count"]))
    extruders = []
    nozzle = extruder.get("nozzleDiameter")
    offsets = extruder.get("offsets") or []
    for i in range(int(extruder.get("count") or 1)):
        values = {}
        if nozzle:
            values["machine_nozzle_size"] = str(nozzle)
        if i < len(offsets) and offsets[i]:
            values["machine_nozzle_offset_x"] = str(offsets[i][0])
            values["machine_nozzle_offset_y"] = str(offsets[i][1])
        extruders.append(values)
    return result, extruders


def merge_machine_config(machine_config, printer_profile, mode="auto"):
    """Adds the OctoPrint printer profile dimensions when needed.

    Machine settings entered explicitly keep priority.
    """
    config = {
        "definition": machine_config.get("definition") or "custom",
        "definition_changes": dict(machine_config.get("definition_changes") or {}),
        "extruders": [dict(e, definition_changes=dict(e.get("definition_changes") or {}))
                      for e in (machine_config.get("extruders") or [])],
    }
    use = mode == "always" or (mode == "auto" and config["definition"] in GENERIC_DEFINITIONS)
    if use and printer_profile:
        global_values, extruder_values = printer_profile_settings(printer_profile)
        for key, value in global_values.items():
            config["definition_changes"].setdefault(key, value)
        while len(config["extruders"]) < len(extruder_values):
            config["extruders"].append({"definition_changes": {}})
        for i, values in enumerate(extruder_values):
            for key, value in values.items():
                config["extruders"][i]["definition_changes"].setdefault(key, value)
    return config


class SliceResult:
    def __init__(self, analysis, warnings, header):
        self.analysis = analysis
        self.warnings = warnings
        self.header = header


def slice_model(library, binary, machine_config, profile, model_path, output_path,
                position=None, overrides=None, on_progress=None, cancel_event=None,
                threads=None, timeout=None, job_name=None, keep_temp=False):
    """Slices ``model_path`` into ``output_path``.

    :param position: {"x": ..., "y": ...} desired center of the model on the bed (machine coordinates).
    """
    started = time.monotonic()
    machine = library.build_machine(machine_config, profile, overrides)
    warnings = list(getattr(machine, "warnings", []))
    global_settings, extruder_settings = build_engine_settings(machine)

    offset_x = offset_y = 0.0
    if position:
        center_is_zero = machine.get("machine_center_is_zero")
        width = float(machine.get("machine_width"))
        depth = float(machine.get("machine_depth"))
        if center_is_zero:
            offset_x, offset_y = float(position.get("x", 0)), float(position.get("y", 0))
        else:
            offset_x = float(position.get("x", width / 2)) - width / 2
            offset_y = float(position.get("y", depth / 2)) - depth / 2

    tmpdir = tempfile.mkdtemp(prefix="curaengine5-")
    try:
        prepared = os.path.join(tmpdir, "model.stl")
        stl.prepare_for_engine(model_path, prepared, offset_x, offset_y)
        out_tmp = os.path.join(tmpdir, "output.gcode")
        fdmprinter = library.loader.find_file("fdmprinter")
        cmd = engine.build_command(binary, fdmprinter, global_settings, extruder_settings, prepared, out_tmp,
                                   threads=threads)
        if on_progress:
            on_progress(0.0)
        parser = engine.run_engine(cmd, on_progress=on_progress, cancel_event=cancel_event, timeout=timeout)
        if not os.path.exists(out_tmp) or os.path.getsize(out_tmp) == 0:
            raise engine.EngineError("CuraEngine produced no G-code", parser.log_tail)

        densities, diameters = [], []
        for extruder in machine.extruders:
            props = extruder.slots["material"].metadata.get("properties") or {}
            densities.append(props.get("density"))
            diameters.append(extruder.get_property("material_diameter", "value"))
        info = engine.finalize_gcode(out_tmp, parser.final_header,
                                     job_name=job_name or os.path.splitext(os.path.basename(model_path))[0],
                                     material_densities=densities, material_diameters=diameters)
        shutil.move(out_tmp, output_path)
    finally:
        if keep_temp:
            _logger.info("Temporary files kept in %s", tmpdir)
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)

    analysis = {}
    if info["print_time"] is not None:
        analysis["estimatedPrintTime"] = info["print_time"]
    if info["filament_m"]:
        analysis["filament"] = {}
        for i, length in enumerate(info["filament_m"]):
            entry = {"length": length * 1000.0}
            try:
                diameter = float(diameters[i])
                entry["volume"] = length * 1000.0 * 3.141592653589793 * (diameter / 2) ** 2 / 1000.0
            except (IndexError, TypeError, ValueError):
                pass
            analysis["filament"]["tool%d" % i] = entry
    _logger.info("Slicing finished in %.1f s", time.monotonic() - started)
    return SliceResult(analysis, warnings, parser.final_header)
