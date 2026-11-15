# SPDX-License-Identifier: AGPL-3.0-or-later
"""Access to Cura resources (definitions, qualities, intents, variants,
materials) and construction of a ``Machine`` ready to be resolved."""
import logging
import os

from .containers import InstanceContainer, parse_cfg
from .definitions import DefinitionLoader
from .machine import Machine
from .materials import MaterialLibrary

_logger = logging.getLogger(__name__)


def _parse_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class _CfgIndex:
    """Index of the .inst.cfg files in a folder (qualities, intents, variants)."""

    def __init__(self, directories):
        self.entries = []
        for directory in directories:
            if not os.path.isdir(directory):
                continue
            for dirpath, _, filenames in os.walk(directory):
                for name in sorted(filenames):
                    if not name.endswith(".inst.cfg"):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        with open(path, encoding="utf-8") as f:
                            general, metadata, _ = parse_cfg(f.read())
                    except Exception as e:  # damaged file: ignore it
                        _logger.debug("Ignoring file %s: %s", path, e)
                        continue
                    self.entries.append({
                        "id": name[: -len(".inst.cfg")],
                        "path": path,
                        "name": general.get("name", ""),
                        "definition": general.get("definition", ""),
                        "metadata": metadata,
                    })

    def find(self, **criteria):
        result = []
        for entry in self.entries:
            ok = True
            for key, wanted in criteria.items():
                actual = entry["definition"] if key == "definition" else entry["metadata"].get(key)
                if callable(wanted):
                    ok = wanted(actual)
                else:
                    ok = actual == wanted
                if not ok:
                    break
            if ok:
                result.append(entry)
        return result


class CuraLibrary:
    """Entry point: resources of a Cura 5.x installation (share/cura/resources)."""

    def __init__(self, resources_dir, extra_dirs=()):
        self.resources_dir = resources_dir
        extra = [d for d in extra_dirs if d]
        self.loader = DefinitionLoader(
            [os.path.join(d, "definitions") for d in extra]
            + [os.path.join(d, "extruders") for d in extra]
            + [os.path.join(resources_dir, "definitions"), os.path.join(resources_dir, "extruders")]
        )
        self.materials = MaterialLibrary([os.path.join(d, "materials") for d in extra] + [os.path.join(resources_dir, "materials")])
        self._quality = None
        self._intent = None
        self._variants = None

    @property
    def quality_index(self):
        if self._quality is None:
            self._quality = _CfgIndex([os.path.join(d, "quality") for d in (self.resources_dir,)])
        return self._quality

    @property
    def intent_index(self):
        if self._intent is None:
            self._intent = _CfgIndex([os.path.join(self.resources_dir, "intent")])
        return self._intent

    @property
    def variant_index(self):
        if self._variants is None:
            self._variants = _CfgIndex([os.path.join(self.resources_dir, "variants")])
        return self._variants

    # --- Lookup -----------------------------------------------------------

    def quality_definition(self, definition):
        if _parse_bool(definition.metadata.get("has_machine_quality", False)):
            return definition.metadata.get("quality_definition", definition.id)
        return "fdmprinter"

    def quality_types(self, machine_id):
        definition = self.loader.load(machine_id)
        qdef = self.quality_definition(definition)
        result = {}
        for entry in self.quality_index.find(definition=qdef, type="quality"):
            qt = entry["metadata"].get("quality_type")
            if qt and qt not in result:
                result[qt] = entry["name"]
        return result

    def _load_cfg(self, entry, definition, slot):
        with open(entry["path"], encoding="utf-8") as f:
            container = InstanceContainer.from_cfg(f.read(), definition, container_id=entry["id"])
        container.type = slot
        return container

    def find_quality(self, definition, quality_type, material=None, variant=None, global_quality=True):
        qdef = self.quality_definition(definition)
        candidates = self.quality_index.find(definition=qdef, type="quality", quality_type=quality_type)
        if global_quality:
            chosen = [c for c in candidates if _parse_bool(c["metadata"].get("global_quality", False))]
        else:
            chosen = []
            for c in candidates:
                meta = c["metadata"]
                if _parse_bool(meta.get("global_quality", False)):
                    continue
                if material and meta.get("material") not in (None, material):
                    continue
                if variant and meta.get("variant") not in (None, variant):
                    continue
                chosen.append(c)
            if not chosen:
                # no extruder-specific quality: Cura uses the global quality
                chosen = [c for c in candidates if _parse_bool(c["metadata"].get("global_quality", False))]
        return chosen[0] if chosen else None

    def find_intent(self, definition, quality_type, intent_category, material=None, variant=None):
        if not intent_category or intent_category == "default":
            return None
        qdef = self.quality_definition(definition)
        for entry in self.intent_index.find(definition=qdef, quality_type=quality_type, intent_category=intent_category):
            meta = entry["metadata"]
            if material and meta.get("material") not in (None, material):
                continue
            if variant and meta.get("variant") not in (None, variant):
                continue
            return entry
        return None

    def find_variant(self, definition, name=None):
        if not _parse_bool(definition.metadata.get("has_variants", False)):
            return None
        name = name or definition.metadata.get("preferred_variant_name")
        entries = self.variant_index.find(definition=definition.id, type="variant",
                                          hardware_type=lambda v: v in (None, "nozzle"))
        for entry in entries:
            if entry["name"] == name:
                return entry
        return entries[0] if entries else None

    def default_material(self, definition):
        return definition.metadata.get("preferred_material", "generic_pla")

    def resolve_material_id(self, definition, material_id, diameter):
        """Finds the material file to use (e.g. "generic_abs" -> "generic_abs_175")."""
        if not material_id:
            return None
        index = self.materials.index
        try:
            thin = diameter is not None and round(float(diameter)) == 2
        except (TypeError, ValueError):
            thin = False
        if thin and not material_id.endswith("_175") and material_id + "_175" in index:
            return material_id + "_175"  # "generic_abs" -> "generic_abs_175" for 1.75 mm
        if material_id in index:
            return material_id
        return None

    # --- Construction -----------------------------------------------------

    def build_machine(self, machine_config, profile=None, overrides=None):
        """Builds a machine.

        :param machine_config: dict with "definition", "definition_changes"
            (global values), "extruders" (list of dicts: "definition_changes",
            "material", "variant", "enabled").
        :param profile: profile dict ("quality_type", "intent_category",
            "global", "extruders", optional "material").
        :param overrides: enforced settings ("user" layer): global dict, and
            optionally "extruders": [dict, ...].
        """
        profile = profile or {}
        overrides = dict(overrides or {})
        definition = self.loader.load(machine_config["definition"])
        quality_type = profile.get("quality_type") or definition.metadata.get("preferred_quality_type", "normal")
        intent_category = profile.get("intent_category", "default")
        warnings = []

        global_containers = {}
        quality = self.find_quality(definition, quality_type)
        if quality is None and profile.get("quality_type"):
            warnings.append('Quality "{}" not found for {}.'.format(quality_type, definition.id))
        if quality is not None:
            global_containers["quality"] = self._load_cfg(quality, definition, "quality")
        global_containers["definition_changes"] = InstanceContainer(
            "global_definition_changes", "definition_changes", definition, values=machine_config.get("definition_changes") or {})
        global_containers["quality_changes"] = InstanceContainer(
            "global_quality_changes", "quality_changes", definition, values=profile.get("global") or {})
        user_extruders = overrides.pop("extruders", None) or []
        global_containers["user"] = InstanceContainer("global_user", "user", definition, values=overrides)

        trains = definition.metadata.get("machine_extruder_trains", {}) or {"0": "fdmextruder"}
        ext_configs = list(machine_config.get("extruders") or [])
        profile_extruders = list(profile.get("extruders") or [])
        extruder_containers = []
        enabled = []
        for position in sorted(trains, key=int):
            index = int(position)
            ext_cfg = ext_configs[index] if index < len(ext_configs) else {}
            enabled.append(ext_cfg.get("enabled", True))
            containers = {}
            # material: profile > machine > preferred material of the definition
            material_id = None
            profile_materials = profile.get("materials") or []
            if index < len(profile_materials) and profile_materials[index]:
                material_id = profile_materials[index]
            elif index == 0 and profile.get("material"):
                material_id = profile["material"]
            elif ext_cfg.get("material"):
                material_id = ext_cfg["material"]
            elif _parse_bool(definition.metadata.get("has_materials", False)):
                material_id = self.default_material(definition)
            diameter = (ext_cfg.get("definition_changes") or {}).get("material_diameter") or \
                (machine_config.get("definition_changes") or {}).get("material_diameter") or \
                definition.get_property("material_diameter", "default_value")
            resolved_material = self.resolve_material_id(definition, material_id, diameter) if material_id else None
            if material_id and resolved_material is None:
                warnings.append('Material "{}" not found.'.format(material_id))
            variant_entry = self.find_variant(definition, ext_cfg.get("variant"))
            variant_name = variant_entry["name"] if variant_entry else None
            if resolved_material:
                material = self.materials.load(resolved_material)
                containers["material"] = InstanceContainer(
                    resolved_material, "material", definition,
                    values=material.values_for(definition.id, variant_name),
                    metadata=material.metadata, name=material.name)
            if variant_entry:
                containers["variant"] = self._load_cfg(variant_entry, definition, "variant")
            ext_quality = self.find_quality(definition, quality_type, material=resolved_material, variant=variant_name, global_quality=False)
            if ext_quality is not None:
                containers["quality"] = self._load_cfg(ext_quality, definition, "quality")
            intent = self.find_intent(definition, quality_type, intent_category, resolved_material, variant_name)
            if intent is not None:
                containers["intent"] = self._load_cfg(intent, definition, "intent")
            elif intent_category not in (None, "", "default"):
                warnings.append('Intent "{}" not found: ignored.'.format(intent_category))
            containers["definition_changes"] = InstanceContainer(
                "ext{}_definition_changes".format(index), "definition_changes", definition,
                values=ext_cfg.get("definition_changes") or {})
            containers["quality_changes"] = InstanceContainer(
                "ext{}_quality_changes".format(index), "quality_changes", definition,
                values=profile_extruders[index] if index < len(profile_extruders) else {})
            containers["user"] = InstanceContainer(
                "ext{}_user".format(index), "user", definition,
                values=user_extruders[index] if index < len(user_extruders) else {})
            extruder_containers.append(containers)

        machine = Machine(self.loader, definition.id, global_containers, extruder_containers, extruders_enabled=enabled)
        machine.warnings = warnings
        return machine
