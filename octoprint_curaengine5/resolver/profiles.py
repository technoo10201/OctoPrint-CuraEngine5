# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import de profils Cura : .curaprofile (zip d'.inst.cfg), .inst.cfg seul, G-code Cura (;SETTING_3)."""
import io
import json
import logging
import re
import zipfile

from .containers import parse_cfg

_logger = logging.getLogger(__name__)

#: Settings version of Cura 5.11 (metadata "setting_version").
SETTING_VERSION = 25

GCODE_SETTING_PREFIX = ";SETTING_3 "
_UNESCAPE = {"\\\\": "\\", "\\n": "\n", "\\r": "\r"}
_UNESCAPE_RE = re.compile("|".join(re.escape(k) for k in _UNESCAPE))


class ProfileError(Exception):
    pass


class ImportedProfile:
    """An imported profile: one global part and one part per extruder."""

    def __init__(self, name="", definition="", quality_type="", intent_category="default",
                 setting_version=None, global_values=None, extruder_values=None):
        self.name = name
        self.definition = definition
        self.quality_type = quality_type
        self.intent_category = intent_category or "default"
        self.setting_version = setting_version
        self.global_values = dict(global_values or {})
        self.extruder_values = [dict(v) for v in (extruder_values or [])]
        self.warnings = []

    def to_dict(self):
        return {
            "name": self.name,
            "definition": self.definition,
            "quality_type": self.quality_type,
            "intent_category": self.intent_category,
            "setting_version": self.setting_version,
            "global": self.global_values,
            "extruders": self.extruder_values,
        }

    def split_single(self, definition):
        """Profile without an extruder part: moves the "settable_per_extruder"
        settings to extruder 0, like CuraContainerRegistry.importProfile."""
        if self.extruder_values:
            return
        extruder = {}
        for key in list(self.global_values):
            if definition.get_property(key, "settable_per_extruder"):
                extruder[key] = self.global_values.pop(key)
        self.extruder_values = [extruder]


def _parse_instance(text):
    general, metadata, values = parse_cfg(text)
    return {
        "name": general.get("name", ""),
        "definition": general.get("definition", ""),
        "metadata": metadata,
        "values": values,
    }


def _assemble(instances, source):
    if not instances:
        raise ProfileError("No profile found in {}".format(source))
    global_profile = None
    extruders = []
    if len(instances) == 1:
        global_profile = instances[0]
    else:
        for inst in instances:
            if not inst["metadata"].get("position"):
                global_profile = inst
            else:
                extruders.append(inst)
    if global_profile is None:
        raise ProfileError("No global part in {}".format(source))
    extruders.sort(key=lambda inst: int(inst["metadata"].get("position", "0")))
    meta = global_profile["metadata"]
    version = meta.get("setting_version")
    profile = ImportedProfile(
        name=global_profile["name"],
        definition=global_profile["definition"],
        quality_type=meta.get("quality_type", ""),
        intent_category=(extruders[0]["metadata"].get("intent_category") if extruders else None) or meta.get("intent_category", "default"),
        setting_version=int(version) if version and str(version).isdigit() else None,
        global_values=global_profile["values"],
        extruder_values=[inst["values"] for inst in extruders],
    )
    if profile.setting_version is not None and profile.setting_version != SETTING_VERSION:
        profile.warnings.append(
            "Profile has settings version {} (Cura 5.11 = {}): some renamed settings "
            "may be ignored.".format(profile.setting_version, SETTING_VERSION)
        )
    if not profile.quality_type:
        profile.warnings.append("The profile does not specify a quality type.")
    return profile


def read_curaprofile(data, source="profile"):
    """Reads a .curaprofile (bytes or path). A file that is not a zip
    is read as a standalone .inst.cfg, as Cura does."""
    if isinstance(data, str):
        with open(data, "rb") as f:
            data = f.read()
        source = source if source != "profile" else "file"
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return _assemble([_parse_instance(data.decode("utf-8"))], source)
    instances = []
    with archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            if info.file_size > 10 * 1024 * 1024:
                raise ProfileError("File {} is too large in the archive".format(info.filename))
            instances.append(_parse_instance(archive.read(info).decode("utf-8")))
    return _assemble(instances, source)


def unescape_gcode_comment(text):
    return _UNESCAPE_RE.sub(lambda m: _UNESCAPE[m.group(0)], text)


def read_gcode_profile(source):
    """Extracts the profile from the ;SETTING_3 block of a G-code produced by Cura.

    :param source: file path, or iterable of lines.
    """
    prefix_length = len(GCODE_SETTING_PREFIX)
    serialized = ""

    def scan(lines):
        result = ""
        for line in lines:
            if line.startswith(GCODE_SETTING_PREFIX):
                result += line[prefix_length:].rstrip("\r\n")
        return result

    if isinstance(source, str):
        with open(source, encoding="utf-8", errors="replace") as f:
            serialized = scan(f)
    else:
        serialized = scan(source)
    serialized = unescape_gcode_comment(serialized).strip()
    if not serialized:
        raise ProfileError("This G-code does not contain a Cura profile (;SETTING_3).")
    try:
        data = json.loads(serialized)
    except ValueError as e:
        raise ProfileError("Unreadable ;SETTING_3 block: {}".format(e))
    instances = [_parse_instance(data["global_quality"])]
    instances[0]["metadata"].pop("position", None)
    for text in data.get("extruder_quality", []):
        inst = _parse_instance(text)
        inst["metadata"].setdefault("position", str(len(instances) - 1))
        instances.append(inst)
    profile = _assemble(instances, "G-code")
    return profile


def write_gcode_profile(profile):
    """Serializes a profile as a ;SETTING_3 block (useful for tests and export)."""

    def cfg(name, definition, metadata, values):
        lines = ["[general]", "version = 4", "name = " + name, "definition = " + definition, "", "[metadata]"]
        lines += ["{} = {}".format(k, v) for k, v in metadata.items()]
        lines += ["", "[values]"]
        for k, v in values.items():
            lines.append("{} = {}".format(k, str(v).replace("\n", "\n\t")))
        return "\n".join(lines) + "\n\n"

    meta = {"type": "quality_changes", "quality_type": profile.quality_type, "setting_version": str(profile.setting_version or SETTING_VERSION)}
    data = {"global_quality": cfg(profile.name, profile.definition, meta, profile.global_values)}
    for position, values in enumerate(profile.extruder_values):
        emeta = dict(meta, position=str(position), intent_category=profile.intent_category)
        data.setdefault("extruder_quality", []).append(cfg(profile.name, profile.definition, emeta, values))
    escaped = json.dumps(data).replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    width = 80 - len(GCODE_SETTING_PREFIX)
    return "".join(GCODE_SETTING_PREFIX + escaped[i:i + width] + "\n" for i in range(0, len(escaped), width))
