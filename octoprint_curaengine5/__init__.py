# SPDX-License-Identifier: AGPL-3.0-or-later
"""OctoPrint-CuraEngine5: slicing with CuraEngine 5.x from OctoPrint.

Unofficial project. "Cura" is a trademark of UltiMaker.
"""
import copy
import json
import logging
import os
import shutil
import threading

import flask
import octoprint.plugin
import octoprint.slicing
from octoprint.access.permissions import Permissions
from octoprint.util import yaml

from . import engine_manager, printers as printers_mod, settings_ui
from .resolver.library import CuraLibrary
from .resolver.profiles import ProfileError, read_curaprofile, read_gcode_profile
from .slicing import engine, stl as stl_mod
from .slicing.job import merge_machine_config, slice_model

__plugin_name__ = "CuraEngine 5 Slicer"
__plugin_pythoncompat__ = ">=3.7,<4"

SLICER_TYPE = "curaengine5"
PROFILE_FORMAT = "curaengine5/1"
TEMP_PROFILE_PREFIX = "_tmp_view_"
MAX_IMPORT_SIZE = 64 * 1024 * 1024
DEFAULT_RESOURCE_DIRS = ["/usr/share/cura/resources", "/usr/local/share/cura/resources"]


def _clean_values(values):
    """Cura setting values as text (the form used in .inst.cfg files)."""
    result = {}
    for key, value in (values or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            value = "True" if value else "False"
        result[str(key)] = str(value)
    return result


class CuraEngine5Plugin(
    octoprint.plugin.SlicerPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.BlueprintPlugin,
):
    def __init__(self):
        super().__init__()
        self._jobs = {}
        self._jobs_lock = threading.Lock()
        self._library = None
        self._library_key = None
        self._library_lock = threading.Lock()
        self._visibility = None
        self._translations = None
        self._ui_resources = None
        self._view_job = None

    # ------------------------------------------------------------------
    # StartupPlugin

    def on_after_startup(self):
        binary = self._engine_binary()
        version = binary.version() if binary else None
        self._logger.info(
            "CuraEngine: %s (version %s), resources: %s",
            binary.path if binary else "not found", version or "?", self._resources_dir() or "not found",
        )

    # ------------------------------------------------------------------
    # SettingsPlugin

    def get_settings_defaults(self):
        return {
            "engine_path": "",
            "resources_path": "",
            "extra_resources_path": "",
            "machine": {
                "definition": "custom",
                "definition_changes": {},
                "extruders": [{"material": "", "variant": "", "definition_changes": {}}],
            },
            "printers": [],
            "default_printer": "",
            "printer_profile_mode": "auto",
            "after_upload": "tab",
            "threads": 0,
            "timeout": 0,
            "keep_temp_files": False,
            "debug_logging": False,
            "engine_download_url": "https://github.com/technoo10201/OctoPrint-CuraEngine5/releases/download/v0.1.0",
        }

    def on_settings_load(self):
        data = octoprint.plugin.SettingsPlugin.on_settings_load(self)
        # Printers and the old "machine" setting: managed by the plugin API.
        for key in ("machine", "printers", "default_printer"):
            data.pop(key, None)
        return data

    def on_settings_save(self, data):
        old_debug = self._settings.get_boolean(["debug_logging"])
        data = dict(data or {})
        for key in ("machine", "machine_json", "printers", "default_printer"):
            data.pop(key, None)
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        self._library = None  # paths or machine may have changed
        new_debug = self._settings.get_boolean(["debug_logging"])
        if old_debug != new_debug:
            level = logging.DEBUG if new_debug else logging.INFO
            logging.getLogger("octoprint.plugins.curaengine5").setLevel(level)
            logging.getLogger(__name__).setLevel(level)

    # ------------------------------------------------------------------
    # TemplatePlugin / AssetPlugin

    def is_template_autoescaped(self):
        return True

    def get_template_configs(self):
        return [
            {"type": "settings", "custom_bindings": True, "name": "CuraEngine 5"},
            {"type": "tab", "custom_bindings": True, "name": "Slicer", "template": "curaengine5_tab.jinja2"},
        ]

    def get_assets(self):
        return {"js": ["js/curaengine5_profile.js", "js/curaengine5.js", "js/curaengine5_tab.js"], "css": ["css/curaengine5.css"]}

    # ------------------------------------------------------------------
    # Engine and resources

    def _engine_path(self):
        configured = self._settings.get(["engine_path"])
        if configured:
            return configured
        candidates = [
            os.path.join(self.get_plugin_data_folder(), "engine", "CuraEngine"),
            shutil.which("CuraEngine"),
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _engine_binary(self):
        path = self._engine_path()
        if not path or not os.path.isfile(path):
            return None
        return engine.EngineBinary.detect(path)

    def _resources_dir(self):
        configured = self._settings.get(["resources_path"])
        candidates = [configured] if configured else []
        candidates.append(os.path.join(self.get_plugin_data_folder(), "resources"))
        engine_path = self._engine_path()
        if engine_path:
            candidates.append(os.path.join(os.path.dirname(engine_path), "share", "cura", "resources"))
        candidates += DEFAULT_RESOURCE_DIRS
        for candidate in candidates:
            if candidate and os.path.isfile(os.path.join(candidate, "definitions", "fdmprinter.def.json")):
                return candidate
        return None

    def _get_library(self):
        resources = self._resources_dir()
        if not resources:
            raise RuntimeError("Cura resources not found (share/cura/resources folder)")
        extra = self._settings.get(["extra_resources_path"]) or ""
        key = (resources, extra)
        with self._library_lock:
            if self._library is None or self._library_key != key:
                self._library = CuraLibrary(resources, [extra] if extra else [])
                self._library_key = key
            return self._library

    @staticmethod
    def _validate_machine(machine):
        if not isinstance(machine, dict):
            raise ValueError("invalid machine configuration")
        extruders = []
        for ext in machine.get("extruders") or [{}]:
            ext = ext if isinstance(ext, dict) else {}
            extruders.append({
                "material": str(ext.get("material") or ""),
                "variant": str(ext.get("variant") or ""),
                "definition_changes": _clean_values(ext.get("definition_changes")),
            })
        return {
            "definition": str(machine.get("definition") or "custom"),
            "definition_changes": _clean_values(machine.get("definition_changes")),
            "extruders": extruders,
        }

    def _machine_config(self):
        machine = self._settings.get(["machine"], merged=True) or {}
        return copy.deepcopy(machine)

    # ------------------------------------------------------------------
    # Printers

    def _printers(self):
        printers = copy.deepcopy(self._settings.get(["printers"]) or [])
        if not printers:
            # old single-machine configuration
            legacy = self._settings.get(["machine"], merged=True) or {}
            if legacy.get("definition") and legacy.get("definition") != "custom" or legacy.get("definition_changes"):
                migrated = printers_mod.migrate_legacy_machine(legacy)
                if migrated:
                    printers = [migrated]
        return printers

    def _default_printer_id(self):
        return self._settings.get(["default_printer"]) or None

    def _save_printers(self, printers, default_id=None):
        self._settings.set(["printers"], printers)
        ids = [p["id"] for p in printers]
        if default_id is None:
            default_id = self._default_printer_id()
        if default_id not in ids:
            default_id = ids[0] if ids else ""
        self._settings.set(["default_printer"], default_id)
        self._settings.save()

    def _find_printer(self, printer_id):
        for printer in self._printers():
            if printer["id"] == printer_id:
                return printer
        return None

    def _sync_printer_profile(self, printer):
        """Create or update the OctoPrint printer profile linked to the printer."""
        manager = getattr(self, "_printer_profile_manager", None)
        if manager is None:
            return
        try:
            manager.save(printers_mod.octoprint_printer_profile(self._get_library(), printer), allow_overwrite=True)
        except Exception:
            self._logger.exception("OctoPrint printer profile not updated for %s", printer["id"])

    def _machine_for_slice(self, profile, printer_profile):
        printer = printers_mod.select_printer(self._printers(), self._default_printer_id(), profile, printer_profile)
        if printer is not None:
            return printers_mod.machine_config(printer), printer
        return self._machine_config(), None

    # ------------------------------------------------------------------
    # SlicerPlugin

    def is_slicer_configured(self):
        binary = self._engine_binary()
        return binary is not None and self._resources_dir() is not None

    def get_slicer_properties(self):
        return {
            "type": SLICER_TYPE,
            "name": "CuraEngine 5",
            "same_device": True,
            "progress_report": True,
            "source_file_types": ["stl"],
            "destination_extensions": ["gcode", "gco", "g"],
        }

    def get_slicer_default_profile(self):
        return octoprint.slicing.SlicingProfile(
            SLICER_TYPE,
            "_default",
            self._normalize_profile({}),
            display_name="Cura (machine default)",
            description="Default quality of the machine definition, unmodified.",
        )

    def get_slicer_profile(self, path):
        data = yaml.load_from_file(path=path) or {}
        display_name = data.pop("_display_name", None)
        description = data.pop("_description", None)
        name = os.path.splitext(os.path.basename(path))[0]
        return octoprint.slicing.SlicingProfile(
            SLICER_TYPE, name, self._normalize_profile(data), display_name=display_name, description=description
        )

    def save_slicer_profile(self, path, profile, allow_overwrite=True, overrides=None):
        if os.path.exists(path) and not allow_overwrite:
            raise IOError("Profile {} already exists and cannot be overwritten".format(profile.name))
        data = self._normalize_profile(copy.deepcopy(profile.data or {}))
        if overrides:
            user = data.setdefault("user", {})
            for key, value in overrides.items():
                if key.startswith("extruder.") and key.count(".") >= 2:
                    # "extruder.<n>.<setting>"
                    _, index, setting = key.split(".", 2)
                    extruders = data.setdefault("user_extruders", [])
                    while len(extruders) <= int(index):
                        extruders.append({})
                    extruders[int(index)][setting] = str(value)
                else:
                    user[key] = "True" if value is True else "False" if value is False else str(value)
        if profile.display_name is not None:
            data["_display_name"] = profile.display_name
        if profile.description is not None:
            data["_description"] = profile.description
        yaml.save_to_file(data, path=path)

    def do_slice(
        self,
        model_path,
        printer_profile,
        machinecode_path=None,
        profile_path=None,
        position=None,
        on_progress=None,
        on_progress_args=None,
        on_progress_kwargs=None,
    ):
        if on_progress_args is None:
            on_progress_args = ()
        if on_progress_kwargs is None:
            on_progress_kwargs = {}
        if not machinecode_path:
            machinecode_path = os.path.splitext(model_path)[0] + ".gco"

        binary = self._engine_binary()
        if binary is None:
            return False, "CuraEngine not found: configure its path in the plugin settings."
        try:
            library = self._get_library()
        except RuntimeError as e:
            return False, str(e)

        if profile_path:
            profile = self.get_slicer_profile(profile_path).data
        else:
            profile = self.get_slicer_default_profile().data
        base_config, printer = self._machine_for_slice(profile, printer_profile)
        if printer is not None:
            self._logger.info("Slicing for printer \"%s\" (%s)", printer["name"], printer["definition"])
        machine_config = merge_machine_config(
            base_config, printer_profile, self._settings.get(["printer_profile_mode"]) or "auto"
        )
        if profile.get("machine") and profile["machine"] != machine_config["definition"]:
            self._logger.warning(
                "The profile was created for %s but the configured machine is %s",
                profile["machine"], machine_config["definition"],
            )
        overrides = dict(profile.get("user") or {})
        if profile.get("user_extruders"):
            overrides["extruders"] = profile["user_extruders"]

        cancel_event = threading.Event()
        with self._jobs_lock:
            self._jobs[machinecode_path] = cancel_event

        def progress(value):
            if on_progress is not None:
                kwargs = dict(on_progress_kwargs)
                kwargs["_progress"] = value
                on_progress(*on_progress_args, **kwargs)

        timeout = (self._settings.get_int(["timeout"]) or 0) * 60 or None
        try:
            result = slice_model(
                library,
                binary,
                machine_config,
                profile,
                model_path,
                machinecode_path,
                position=position,
                overrides=overrides,
                on_progress=progress,
                cancel_event=cancel_event,
                threads=self._settings.get_int(["threads"]) or None,
                timeout=timeout,
                keep_temp=self._settings.get_boolean(["keep_temp_files"]),
            )
        except engine.SlicingCancelled:
            self._logger.info("Slicing of %s cancelled", model_path)
            raise octoprint.slicing.SlicingCancelled()
        except engine.EngineError as e:
            self._logger.error("CuraEngine failed: %s\n%s", e, "\n".join(e.log_tail))
            tail = [l for l in e.log_tail if "[error]" in l or "[critical]" in l]
            return False, "{} {}".format(e, " / ".join(tail[-3:])).strip()
        except Exception as e:
            self._logger.exception("Error while slicing %s", model_path)
            return False, "Error while slicing: {}".format(e)
        finally:
            with self._jobs_lock:
                self._jobs.pop(machinecode_path, None)

        for warning in result.warnings:
            self._logger.warning(warning)
        return True, {"analysis": result.analysis}

    def cancel_slicing(self, machinecode_path):
        with self._jobs_lock:
            event = self._jobs.get(machinecode_path)
        if event is not None:
            event.set()

    # ------------------------------------------------------------------
    # Profiles

    @staticmethod
    def _normalize_profile(data):
        data = dict(data or {})
        data.setdefault("format", PROFILE_FORMAT)
        data.setdefault("machine", None)
        data.setdefault("printer", None)
        data.setdefault("quality_type", None)
        data.setdefault("intent_category", "default")
        data.setdefault("material", None)
        data["global"] = _clean_values(data.get("global"))
        data["extruders"] = [_clean_values(e) for e in (data.get("extruders") or [])]
        data["user"] = _clean_values(data.get("user"))
        data["user_extruders"] = [_clean_values(e) for e in (data.get("user_extruders") or [])]
        return data

    def _profile_from_import(self, imported, material=None, printer_id=None):
        printers = self._printers()
        printer = next((p for p in printers if p["id"] == printer_id), None)
        if printer is None:
            # printer with the same definition as the profile, otherwise the default one
            printer = next((p for p in printers if p["definition"] == imported.definition), None) or \
                printers_mod.select_printer(printers, self._default_printer_id())
        definition_id = printer["definition"] if printer else (self._machine_config().get("definition") or "custom")
        try:
            definition = self._get_library().loader.load(definition_id)
            if not imported.extruder_values:
                imported.split_single(definition)
        except Exception as e:
            self._logger.warning("Could not split the profile: %s", e)
        data = {
            "format": PROFILE_FORMAT,
            "machine": imported.definition or None,
            "printer": printer["id"] if printer else None,
            "quality_type": imported.quality_type or None,
            "intent_category": imported.intent_category,
            "material": material or None,
            "global": imported.global_values,
            "extruders": imported.extruder_values,
        }
        return self._normalize_profile(data)

    # ------------------------------------------------------------------
    # API (BlueprintPlugin)

    def is_blueprint_protected(self):
        return True

    def is_blueprint_csrf_protected(self):
        return True

    @octoprint.plugin.BlueprintPlugin.route("/status", methods=["GET"])
    def api_status(self):
        binary = self._engine_binary()
        resources = self._resources_dir()
        return flask.jsonify(
            {
                "engine_path": binary.path if binary else None,
                "engine_version": binary.version() if binary else None,
                "resources_path": resources,
                "configured": binary is not None and resources is not None,
            }
        )

    @octoprint.plugin.BlueprintPlugin.route("/machines", methods=["GET"])
    def api_machines(self):
        try:
            library = self._get_library()
        except RuntimeError as e:
            return flask.jsonify({"error": str(e), "machines": []})
        machines = [
            {"id": machine_id, "name": name, "manufacturer": manufacturer or "Other"}
            for machine_id, name, visible, manufacturer in library.loader.list_machines()
            if visible or machine_id in ("custom",)
        ]
        machines.sort(key=lambda m: (m["manufacturer"].lower(), m["name"].lower()))
        return flask.jsonify({"machines": machines})

    @octoprint.plugin.BlueprintPlugin.route("/machine/<definition_id>", methods=["GET"])
    def api_machine(self, definition_id):
        try:
            library = self._get_library()
            definition = library.loader.load(definition_id)
        except Exception as e:
            return flask.jsonify({"error": str(e)}), 404
        machine = library.build_machine({"definition": definition_id})
        keys = ["machine_width", "machine_depth", "machine_height", "machine_heated_bed", "machine_center_is_zero",
                "machine_gcode_flavor", "machine_start_gcode", "machine_end_gcode", "material_diameter"]
        return flask.jsonify(
            {
                "id": definition_id,
                "name": definition.name,
                "defaults": {k: machine.get(k) for k in keys},
                "nozzle_size": machine.get("machine_nozzle_size", 0),
                "quality_types": library.quality_types(definition_id),
                "preferred_material": definition.metadata.get("preferred_material"),
                "has_materials": str(definition.metadata.get("has_materials", False)).lower() == "true",
            }
        )

    @octoprint.plugin.BlueprintPlugin.route("/materials", methods=["GET"])
    def api_materials(self):
        try:
            library = self._get_library()
        except RuntimeError as e:
            return flask.jsonify({"error": str(e), "materials": []})
        diameter = flask.request.args.get("diameter", type=float)
        materials = [
            {"id": m.id, "name": m.name, "brand": m.metadata.get("brand", ""), "type": m.metadata.get("material", "")}
            for m in library.materials.list(diameter=diameter)
        ]
        return flask.jsonify({"materials": materials})

    # --- Printers -------------------------------------------------

    def _printer_summary(self, printer):
        return {"id": printer["id"], "name": printer["name"], "definition": printer["definition"],
                "printer_profile_id": printer.get("printer_profile_id"),
                "default_profile": printer.get("default_profile") or "",
                "default": printer["id"] == (self._default_printer_id() or "")}

    def _printer_detail(self, library, printer):
        data = self._printer_summary(printer)
        data["settings"] = printers_mod.machine_settings(library, printer)
        data["material"] = ((printer.get("extruders") or [{}])[0] or {}).get("material", "")
        data["material_diameter"] = data["settings"]["extruder"]["material_diameter"]["value"]
        return data

    @octoprint.plugin.BlueprintPlugin.route("/catalog", methods=["GET"])
    def api_catalog(self):
        try:
            library = self._get_library()
        except RuntimeError as e:
            return flask.jsonify({"error": str(e), "machines": []})
        machines = library.loader.catalog()
        machines.sort(key=lambda m: (m["manufacturer"].lower(), m["name"].lower()))
        return flask.jsonify({"machines": machines})

    @octoprint.plugin.BlueprintPlugin.route("/printers", methods=["GET"])
    def api_printers(self):
        return flask.jsonify({"printers": [self._printer_summary(p) for p in self._printers()]})

    @octoprint.plugin.BlueprintPlugin.route("/printers", methods=["POST"])
    @Permissions.SETTINGS.require(403)
    def api_printer_add(self):
        data = flask.request.get_json(silent=True) or {}
        try:
            library = self._get_library()
            printers = self._printers()
            printer = printers_mod.new_printer(
                library, str(data.get("definition") or ""), data.get("name"), [p["id"] for p in printers])
        except Exception as e:
            return flask.make_response("Could not create printer: {}".format(e), 400)
        printers.append(printer)
        self._save_printers(printers)
        self._sync_printer_profile(printer)
        return flask.jsonify(self._printer_detail(library, printer))

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>", methods=["GET"])
    def api_printer(self, printer_id):
        printer = self._find_printer(printer_id)
        if printer is None:
            return flask.make_response("Unknown printer", 404)
        return flask.jsonify(self._printer_detail(self._get_library(), printer))

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>", methods=["PUT"])
    @Permissions.SETTINGS.require(403)
    def api_printer_update(self, printer_id):
        data = flask.request.get_json(silent=True) or {}
        printers = self._printers()
        index = next((i for i, p in enumerate(printers) if p["id"] == printer_id), None)
        if index is None:
            return flask.make_response("Unknown printer", 404)
        library = self._get_library()
        try:
            updated = printers_mod.apply_machine_settings(
                library, printers[index], data.get("global"), data.get("extruder"),
                material=data.get("material"), name=data.get("name"))
            library.build_machine(printers_mod.machine_config(updated)).get("machine_width")
        except Exception as e:
            return flask.make_response("Values rejected: {}".format(e), 400)
        if "default_profile" in data:
            wanted = str(data.get("default_profile") or "")
            if wanted and wanted not in self._usable_profiles(printer_id):
                return flask.make_response("Unknown profile for this printer", 400)
            updated["default_profile"] = wanted
        printers[index] = updated
        self._save_printers(printers, default_id=printer_id if data.get("default") else None)
        self._sync_printer_profile(updated)
        return flask.jsonify(self._printer_detail(library, updated))

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>", methods=["DELETE"])
    @Permissions.SETTINGS.require(403)
    def api_printer_delete(self, printer_id):
        printers = self._printers()
        printer = next((p for p in printers if p["id"] == printer_id), None)
        if printer is None:
            return flask.make_response("Unknown printer", 404)
        for name in list(self._printer_slicing_profiles(printer_id)):
            self._slicing_manager.delete_profile(SLICER_TYPE, name)
        manager = getattr(self, "_printer_profile_manager", None)
        if manager is not None and printer.get("printer_profile_id"):
            try:
                manager.remove(printer["printer_profile_id"])
            except Exception:
                self._logger.exception("OctoPrint printer profile not removed")
        self._save_printers([p for p in printers if p["id"] != printer_id])
        return flask.jsonify({"deleted": printer_id})

    # --- Print profiles of a printer -----------------------

    def _printer_slicing_profiles(self, printer_id):
        result = {}
        for name, profile in self._slicing_manager.all_profiles(SLICER_TYPE).items():
            if (profile.data or {}).get("printer") == printer_id and not name.startswith(TEMP_PROFILE_PREFIX):
                result[name] = profile
        return result

    def _usable_profiles(self, printer_id):
        """Profiles of the printer + imported profiles without a printer (usable with any of them)."""
        result = {}
        for name, profile in self._slicing_manager.all_profiles(SLICER_TYPE).items():
            owner = (profile.data or {}).get("printer")
            if (owner == printer_id or not owner) and not name.startswith(TEMP_PROFILE_PREFIX):
                result[name] = profile
        return result

    @staticmethod
    def _profile_view(name, profile):
        data = profile.data or {}

        def self_flat(d):
            flat = settings_ui.flatten_overrides(d)
            if d.get("printer"):
                return flat
            # imported profile: also expose its base values so it can be duplicated for a printer
            base = dict(d.get("global") or {})
            if d.get("extruders"):
                base.update(d["extruders"][0] or {})
            base.update(flat)
            return base
        return {"name": name, "display_name": profile.display_name or name, "printer": data.get("printer"),
                "material": data.get("material") or "", "quality_type": data.get("quality_type") or "",
                "user": self_flat(data)}

    def _profile_defaults(self, library, printer, material, quality_type):
        machine = library.build_machine(
            printers_mod.machine_config(printer), {"quality_type": quality_type or None, "material": material or None})
        values = {}
        for key, _label, _kind in printers_mod.SIMPLE_SETTINGS:
            try:
                values[key] = machine.get(key, extruder=0)
            except Exception:
                values[key] = None
        return values, machine.warnings

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>/options", methods=["GET"])
    def api_printer_options(self, printer_id):
        """Materials, qualities and default values for the profile editor."""
        printer = self._find_printer(printer_id)
        if printer is None:
            return flask.make_response("Unknown printer", 404)
        library = self._get_library()
        definition = library.loader.load(printer["definition"])
        material = flask.request.args.get("material") or ((printer.get("extruders") or [{}])[0].get("material"))
        quality = flask.request.args.get("quality_type") or definition.metadata.get("preferred_quality_type", "normal")
        diameter = library.build_machine(printers_mod.machine_config(printer)).get("material_diameter", 0)
        materials = [{"id": m.id, "name": m.name, "brand": m.metadata.get("brand", ""), "type": m.metadata.get("material", "")}
                     for m in library.materials.list(diameter=diameter)]
        defaults, warnings = self._profile_defaults(library, printer, material, quality)
        return flask.jsonify({
            "materials": materials,
            "quality_types": [{"id": k, "name": v} for k, v in library.quality_types(printer["definition"]).items()],
            "settings": [{"key": k, "label": l, "kind": kind} for k, l, kind in printers_mod.SIMPLE_SETTINGS],
            "defaults": defaults, "warnings": warnings, "quality_type": quality, "material": material or "",
        })

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>/profiles", methods=["GET"])
    def api_printer_profiles(self, printer_id):
        profiles = self._usable_profiles(printer_id) if flask.request.args.get("shared") else \
            self._printer_slicing_profiles(printer_id)
        return flask.jsonify({"profiles": [self._profile_view(n, p) for n, p in sorted(profiles.items())]})

    def _printer_profile_data(self, printer, data):
        definition = self._get_library().loader.load(printer["definition"])
        flat, _errors = settings_ui.sanitize_overrides(definition, data.get("user"))
        global_values, extruder_values = settings_ui.split_overrides(definition, flat)
        return self._normalize_profile({
            "machine": printer["definition"], "printer": printer["id"],
            "quality_type": data.get("quality_type") or None, "material": data.get("material") or None,
            "user": global_values, "user_extruders": [extruder_values] if extruder_values else [],
        })

    def _save_printer_profile(self, printer, name, data, display_name, allow_overwrite):
        profile = octoprint.slicing.SlicingProfile(
            SLICER_TYPE, name, self._printer_profile_data(printer, data), display_name=display_name)
        self._slicing_manager.save_profile(SLICER_TYPE, name, profile, allow_overwrite=allow_overwrite,
                                           display_name=display_name)

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>/profiles", methods=["POST"])
    @Permissions.SETTINGS.require(403)
    def api_printer_profile_add(self, printer_id):
        printer = self._find_printer(printer_id)
        if printer is None:
            return flask.make_response("Unknown printer", 404)
        data = flask.request.get_json(silent=True) or {}
        display_name = (data.get("display_name") or "").strip() or "Profile"
        existing = set(self._slicing_manager.all_profiles(SLICER_TYPE))
        name = printers_mod.unique_id(printer_id + "_" + printers_mod.slugify(display_name), existing)
        try:
            self._save_printer_profile(printer, name, data, display_name, allow_overwrite=False)
        except IOError as e:
            return flask.make_response(str(e), 409)
        return flask.jsonify({"name": name})

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>/profiles/<name>", methods=["PUT"])
    @Permissions.SETTINGS.require(403)
    def api_printer_profile_update(self, printer_id, name):
        printer = self._find_printer(printer_id)
        if printer is None or name not in self._printer_slicing_profiles(printer_id):
            return flask.make_response("Unknown profile", 404)
        data = flask.request.get_json(silent=True) or {}
        display_name = (data.get("display_name") or "").strip() or name
        self._save_printer_profile(printer, name, data, display_name, allow_overwrite=True)
        return flask.jsonify({"name": name})

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>/profiles/<name>", methods=["DELETE"])
    @Permissions.SETTINGS.require(403)
    def api_printer_profile_delete(self, printer_id, name):
        if name not in self._printer_slicing_profiles(printer_id):
            return flask.make_response("Unknown profile", 404)
        self._slicing_manager.delete_profile(SLICER_TYPE, name)
        return flask.jsonify({"deleted": name})

    # --- Cura-style settings pane --------------------------------

    def _ui_support(self):
        library = self._get_library()
        if self._visibility is None or self._translations is None or self._ui_resources != library.resources_dir:
            self._visibility = settings_ui.read_visibility(library.resources_dir)
            self._translations = settings_ui.Translations(library.resources_dir)
            self._ui_resources = library.resources_dir
        return library, self._visibility, self._translations

    @staticmethod
    def _request_lang(body):
        lang = body.get("lang")
        if not lang:
            locale = getattr(flask.g, "locale", None)
            lang = str(locale) if locale else "en"
        return lang

    def _ui_machines(self, library, printer, body):
        """(machine with overrides, base machine) for the requested material and quality."""
        definition = library.loader.load(printer["definition"])
        profile = {"quality_type": body.get("quality_type") or None, "material": body.get("material") or None}
        flat, errors = settings_ui.sanitize_overrides(definition, body.get("overrides"))
        global_values, extruder_values = settings_ui.split_overrides(definition, flat)
        overrides = dict(global_values)
        if extruder_values:
            overrides["extruders"] = [extruder_values]
        config = printers_mod.machine_config(printer)
        machine = library.build_machine(config, profile, overrides)
        base = library.build_machine(config, profile)
        return definition, machine, base, errors

    def _ui_rows(self, library, printer, body, keys, with_structure, visibility, translations):
        definition, machine, base, errors = self._ui_machines(library, printer, body)
        lang = self._request_lang(body)
        rows = []
        for key, depth in keys:
            if key not in definition.settings:
                continue
            row = {"key": key}
            if with_structure:
                row.update(settings_ui.row_structure(definition, key, depth, visibility, translations.get(lang)))
            try:
                row.update(settings_ui.row_values(machine, base, key, definition, translations.get(lang)))
            except Exception as e:
                self._logger.debug("Value of %s unavailable: %s", key, e)
                continue
            if key in errors:
                row["error"] = settings_ui.MESSAGES["invalid"]
            rows.append(row)
        return rows

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>/ui/categories", methods=["POST"])
    def api_ui_categories(self, printer_id):
        printer = self._find_printer(printer_id)
        if printer is None:
            return flask.make_response("Unknown printer", 404)
        body = flask.request.get_json(silent=True) or {}
        library, visibility, translations = self._ui_support()
        level = body.get("level") if body.get("level") in settings_ui.LEVELS else "basic"
        definition = library.loader.load(printer["definition"])
        return flask.jsonify({"categories": settings_ui.categories(
            definition, visibility, level, translations.get(self._request_lang(body)))})

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>/ui/rows", methods=["POST"])
    def api_ui_rows(self, printer_id):
        """Structure and values of a category, a search or a list of settings."""
        printer = self._find_printer(printer_id)
        if printer is None:
            return flask.make_response("Unknown printer", 404)
        body = flask.request.get_json(silent=True) or {}
        library, visibility, translations = self._ui_support()
        level = body.get("level") if body.get("level") in settings_ui.LEVELS else "basic"
        definition = library.loader.load(printer["definition"])
        if body.get("recommended"):
            keys = [(k, 0) for k in settings_ui.RECOMMENDED_KEYS]
        elif body.get("search"):
            keys = settings_ui.search_keys(definition, body["search"], visibility, level,
                                           translations.get(self._request_lang(body)))
        else:
            keys = settings_ui.category_keys(definition, body.get("category") or "", visibility, level)
        try:
            rows = self._ui_rows(library, printer, body, keys, True, visibility, translations)
        except Exception as e:
            return flask.make_response("Computation failed: {}".format(e), 400)
        return flask.jsonify({"rows": rows})

    @octoprint.plugin.BlueprintPlugin.route("/printers/<printer_id>/ui/values", methods=["POST"])
    def api_ui_values(self, printer_id):
        """Light recomputation: only values, "enabled" state and warnings of the requested settings."""
        printer = self._find_printer(printer_id)
        if printer is None:
            return flask.make_response("Unknown printer", 404)
        body = flask.request.get_json(silent=True) or {}
        library, visibility, translations = self._ui_support()
        keys = [(str(k), 0) for k in (body.get("keys") or [])][:200]
        try:
            rows = self._ui_rows(library, printer, body, keys, False, visibility, translations)
        except Exception as e:
            return flask.make_response("Computation failed: {}".format(e), 400)
        return flask.jsonify({"values": {r.pop("key"): r for r in rows}})

    # --- "Slicer" tab: slicing a positioned model ----------------

    @octoprint.plugin.BlueprintPlugin.route("/slice", methods=["POST"])
    @Permissions.SETTINGS.require(403)
    def api_slice(self):
        """Slice an STL from the OctoPrint files with the 3D view transform.

        JSON: ``file`` (local path), ``printer``, ``profile`` (optional), ``gcode`` (output name),
        ``transform``: ``position`` {x, y} (pivot, machine coordinates), ``rotation`` {x, y, z} in degrees,
        ``scale`` {x, y, z} in %. The model always rests on the build plate.
        """
        import tempfile

        from octoprint.filemanager import FileDestinations
        from octoprint.filemanager.util import DiskFileWrapper

        if self._printer is not None and (self._printer.is_printing() or self._printer.is_paused()):
            return flask.make_response("Slicing is disabled while printing", 409)
        body = flask.request.get_json(silent=True) or {}
        printer = self._find_printer(body.get("printer") or "")
        if printer is None:
            return flask.make_response("Choose a printer", 400)
        if not self.is_slicer_configured():
            return flask.make_response("CuraEngine or resources not found", 400)
        try:
            source = self._file_manager.path_on_disk(FileDestinations.LOCAL, body.get("file") or "")
        except Exception:
            return flask.make_response("File not found", 404)
        if not os.path.isfile(source):
            return flask.make_response("File not found", 404)
        profile_name = body.get("profile") or None
        if profile_name and profile_name not in self._usable_profiles(printer["id"]):
            return flask.make_response("Unknown profile for this printer", 400)
        edited = body.get("edited")
        temp_profile = None
        transform = body.get("transform") or {}
        try:
            vec = lambda d, default: tuple(float((d or {}).get(k, default)) for k in "xyz")  # noqa: E731
            rotation, scale = vec(transform.get("rotation"), 0), vec(transform.get("scale"), 100)
            position = transform.get("position") or {}
            px, py = float(position.get("x", 0)), float(position.get("y", 0))
            if any(v <= 0 for v in scale):
                raise ValueError("scale")
        except (TypeError, ValueError):
            return flask.make_response("Invalid transform", 400)

        # the G-code always carries the STL's name (same folder): model.stl -> model.gcode
        name = os.path.splitext(str(body.get("file") or ""))[0] + ".gcode"
        current_file = ((self._printer.get_current_job() or {}).get("file") or {}) if self._printer else {}
        if current_file.get("origin") == "local" and current_file.get("path") == name:
            return flask.make_response(flask.jsonify({
                "busy": True, "gcode": name,
                "error": "{} is selected or being printed: it cannot be overwritten. "
                         "Deselect it or wait for the print to finish.".format(name)}), 409)
        if not body.get("overwrite") and self._file_manager.file_exists(FileDestinations.LOCAL, name):
            return flask.make_response(flask.jsonify({"exists": True, "gcode": name}), 409)

        overrides = {}
        supports = body.get("supports")
        if supports in ("everywhere", "buildplate"):
            overrides = {"support_enable": True, "support_type": supports}
        elif supports == "none":
            overrides = {"support_enable": False}

        workdir = tempfile.mkdtemp(prefix="curaengine5-view-", dir=self.get_plugin_data_folder())
        baked = os.path.join(workdir, "model.stl")
        try:
            cx, cy = stl_mod.transform_model(source, baked, rotation, scale)
        except (stl_mod.StlError, OSError) as e:
            shutil.rmtree(workdir, ignore_errors=True)
            return flask.make_response("Unreadable STL: {}".format(e), 400)
        out_path = os.path.join(workdir, "output.gcode")
        started = {"printer": printer["name"], "gcode": name}
        with self._jobs_lock:
            if self._view_job is not None:
                shutil.rmtree(workdir, ignore_errors=True)
                return flask.make_response("A slicing job is already running", 409)
            self._view_job = out_path

        def done(*args, **kwargs):
            message = dict(started, type="slice_done")
            try:
                if kwargs.get("_error"):
                    message.update(ok=False, error=str(kwargs["_error"]))
                elif kwargs.get("_cancelled"):
                    message.update(ok=False, error="Slicing cancelled")
                else:
                    wrapper = DiskFileWrapper(name, out_path)
                    added = self._file_manager.add_file(
                        FileDestinations.LOCAL, name, wrapper, allow_overwrite=True,
                        analysis=kwargs.get("_analysis"))
                    message.update(ok=True, gcode=added if isinstance(added, str) else name)
            except Exception as e:
                self._logger.exception("Could not save the G-code")
                message.update(ok=False, error="Could not save: {}".format(e))
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
                with self._jobs_lock:
                    self._view_job = None
                if temp_profile:
                    try:
                        self._slicing_manager.delete_profile(SLICER_TYPE, temp_profile)
                    except Exception:
                        self._logger.debug("Temporary profile %s not removed", temp_profile)
            self._plugin_manager.send_plugin_message(self._identifier, message)

        if isinstance(edited, dict):
            # settings edited in the tab and not saved: temporary profile, deleted after slicing
            import time as _time

            temp_profile = "{}{}".format(TEMP_PROFILE_PREFIX, int(_time.time() * 1000))
            try:
                self._save_printer_profile(printer, temp_profile, edited, "Modified settings", True)
            except Exception as e:
                with self._jobs_lock:
                    self._view_job = None
                shutil.rmtree(workdir, ignore_errors=True)
                return flask.make_response("Invalid settings: {}".format(e), 400)
            profile_name = temp_profile
        last = {"value": -1}

        def progress(*args, **kwargs):
            percent = int(max(0.0, min(1.0, float(kwargs.get("_progress", 0)))) * 100)
            if percent != last["value"]:
                last["value"] = percent
                stage = ("Preparing" if percent < 5 else "Slicing layers" if percent < 10 else
                         "Walls and infill" if percent < 50 else "Supports" if percent < 60 else
                         "Writing G-code")
                self._plugin_manager.send_plugin_message(
                    self._identifier, {"type": "slice_progress", "gcode": name, "progress": percent, "stage": stage})

        try:
            self._slicing_manager.slice(
                SLICER_TYPE, baked, out_path, profile_name, done, on_progress=progress, overrides=overrides or None,
                printer_profile_id=printer.get("printer_profile_id"),
                position={"x": px + cx, "y": py + cy})
        except Exception as e:
            shutil.rmtree(workdir, ignore_errors=True)
            with self._jobs_lock:
                self._view_job = None
            if temp_profile:
                self._slicing_manager.delete_profile(SLICER_TYPE, temp_profile)
            return flask.make_response("Slicing failed: {}".format(e), 400)
        return flask.jsonify({"started": True, "gcode": name})

    @octoprint.plugin.BlueprintPlugin.route("/slice/cancel", methods=["POST"])
    @Permissions.SETTINGS.require(403)
    def api_slice_cancel(self):
        with self._jobs_lock:
            job = self._view_job
        if job is None:
            return flask.make_response("No slicing job running", 404)
        self.cancel_slicing(job)
        return flask.jsonify({"cancelling": True})

    @octoprint.plugin.BlueprintPlugin.route("/import", methods=["POST"])
    @Permissions.SETTINGS.require(403)
    def api_import(self):
        """Import a Cura profile (.curaprofile, .inst.cfg or G-code produced by Cura).

        Multipart form: ``file``, ``name`` (identifier), ``display_name``,
        ``description``, ``material``, ``default``; or JSON with the ``path`` of a
        G-code in OctoPrint's local storage.
        """
        upload_field = "file." + self._settings.global_get(["server", "uploads", "pathSuffix"])
        name_field = "file." + self._settings.global_get(["server", "uploads", "nameSuffix"])
        values = flask.request.values
        json_data = flask.request.get_json(silent=True) or {}

        try:
            if upload_field in values:
                path = values[upload_field]
                filename = values.get(name_field, "profile")
                imported = self._read_profile_file(path, filename)
                params = values
            elif json_data.get("path"):
                path = self._file_manager.path_on_disk("local", json_data["path"])
                filename = os.path.basename(path)
                imported = read_gcode_profile(path)
                params = json_data
            else:
                return flask.make_response("No file provided", 400)
        except (ProfileError, ValueError, OSError, UnicodeDecodeError) as e:
            return flask.make_response("Unreadable profile: {}".format(e), 400)

        name = params.get("name") or os.path.splitext(filename)[0]
        name = _slugify(name)
        display_name = params.get("display_name") or imported.name or name
        description = params.get("description") or "Imported from {}".format(filename)
        data = self._profile_from_import(imported, material=params.get("material") or None,
                                         printer_id=params.get("printer") or None)
        profile = octoprint.slicing.SlicingProfile(SLICER_TYPE, name, data, display_name=display_name, description=description)
        make_default = str(params.get("default", "false")).lower() in ("true", "1", "on", "yes")
        allow_overwrite = str(params.get("allow_overwrite", "false")).lower() in ("true", "1", "on", "yes")
        try:
            self._slicing_manager.save_profile(
                SLICER_TYPE, name, profile, allow_overwrite=allow_overwrite,
                display_name=display_name, description=description,
            )
        except IOError as e:
            return flask.make_response(str(e), 409)
        if make_default:
            self._slicing_manager.set_default_profile(SLICER_TYPE, name, require_exists=True)
        return flask.jsonify(
            {
                "name": name,
                "display_name": display_name,
                "quality_type": imported.quality_type,
                "machine": imported.definition,
                "settings": len(data["global"]) + sum(len(e) for e in data["extruders"]),
                "warnings": imported.warnings,
            }
        )

    @octoprint.plugin.BlueprintPlugin.route("/engine", methods=["GET"])
    def api_engine_platform(self):
        arch, problem = engine_manager.check_platform()
        return flask.jsonify({
            "arch": arch,
            "problem": problem,
            "download_url": self._settings.get(["engine_download_url"]) or None,
            "version": engine_manager.ENGINE_VERSION,
        })

    @octoprint.plugin.BlueprintPlugin.route("/engine/install", methods=["POST"])
    @Permissions.ADMIN.require(403)
    def api_engine_install(self):
        """Download and install the prebuilt engine (manual action by an administrator)."""
        base_url = self._settings.get(["engine_download_url"])
        if not base_url:
            return flask.make_response("No download URL configured", 400)
        try:
            engine_path, resources = engine_manager.download_and_install(base_url, self.get_plugin_data_folder())
        except engine_manager.InstallError as e:
            return flask.make_response(str(e), 400)
        except Exception as e:
            self._logger.exception("Could not install the engine")
            return flask.make_response("Installation failed: {}".format(e), 500)
        self._library = None
        binary = self._engine_binary()
        return flask.jsonify({"engine_path": engine_path, "resources_path": resources,
                              "engine_version": binary.version() if binary else None})

    @staticmethod
    def _read_profile_file(path, filename):
        lower = filename.lower()
        if lower.endswith((".gcode", ".gco", ".g")):
            return read_gcode_profile(path)
        with open(path, "rb") as f:
            data = f.read(MAX_IMPORT_SIZE + 1)
        if len(data) > MAX_IMPORT_SIZE:
            raise ProfileError("File too large")
        if data.lstrip().startswith(b";") or b"\n;SETTING_3 " in data:
            import io

            return read_gcode_profile(io.StringIO(data.decode("utf-8", errors="replace")))
        return read_curaprofile(data, source=filename)

    def bodysize_hook(self, current_max_body_sizes, *args, **kwargs):
        return [("POST", r"/import", MAX_IMPORT_SIZE)]


def _jsonable(value):
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _slugify(name):
    import re

    slug = re.sub(r"[^a-z0-9_\-]+", "_", name.lower()).strip("_")
    return slug or "profile"


__plugin_implementation__ = CuraEngine5Plugin()
__plugin_hooks__ = {
    "octoprint.server.http.bodysize": __plugin_implementation__.bodysize_hook,
    # TODO: add "octoprint.plugin.softwareupdate.check_config" once the repository is public.
}
