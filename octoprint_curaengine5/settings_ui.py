# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cura-style settings pane: settings tree, visibility, translations, recomputed values.

Module with no dependency on OctoPrint or Flask (testable on its own).
"""
import ast
import os
import re
import threading

LEVELS = ("basic", "advanced", "expert")
#: Categories never offered in the profile panel.
HIDDEN_CATEGORIES = {"machine_settings", "command_line_settings"}
#: Settings of the "Recommended" mode.
RECOMMENDED_KEYS = ["layer_height", "infill_sparse_density", "support_enable", "adhesion_type"]
EDITABLE_TYPES = {"float", "int", "bool", "enum", "str"}

MESSAGES = {
    "min": "Value too low: minimum is {0}.", "max": "Value too high: maximum is {0}.",
    "min_w": "Low value, the recommended minimum is {0}.", "max_w": "High value, the recommended maximum is {0}.",
    "invalid": "Invalid value.",
}


# --- Visibility --------------------------------------------------------------

def read_visibility(resources):
    """{level: set of keys} from Cura's setting_visibility files."""
    result = {}
    for level in LEVELS:
        keys = set()
        path = os.path.join(resources, "setting_visibility", level + ".cfg")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith(("[", "#")) and "=" not in line:
                        keys.add(line)
        result[level] = keys
    return result


def level_of(visibility, key):
    for level in LEVELS:
        if key in visibility.get(level, ()):
            return level
    return "expert"


def is_visible(visibility, key, level):
    return LEVELS.index(level_of(visibility, key)) <= LEVELS.index(level)


# --- Translations (Cura .po files) ---------------------------------------------------

def parse_po(path):
    """Read a .po file: {msgctxt: msgstr} (translated entries only)."""
    entries, ctx, field, buf = {}, None, None, {"msgid": "", "msgstr": ""}

    def unquote(text):
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text.strip('"')

    def flush():
        if ctx is not None and buf["msgstr"]:
            entries[ctx] = buf["msgstr"]

    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("msgctxt"):
                flush()
                ctx, field, buf = unquote(line[7:].strip()), "msgctxt", {"msgid": "", "msgstr": ""}
            elif line.startswith("msgid"):
                field = "msgid"
                buf["msgid"] = unquote(line[5:].strip())
            elif line.startswith("msgstr"):
                field = "msgstr"
                buf["msgstr"] = unquote(line[6:].strip())
            elif line.startswith('"') and field in ("msgid", "msgstr"):
                buf[field] += unquote(line)
    flush()
    return entries


class Translations:
    """fdmprinter translations for the OctoPrint language ("fr", "de", "en"...)."""

    def __init__(self, resources):
        self.resources = resources
        self._cache = {}
        self._lock = threading.Lock()

    def directory_for(self, lang):
        lang = (lang or "en").replace("-", "_")
        base = os.path.join(self.resources, "i18n")
        if lang.lower().startswith("en") or not os.path.isdir(base):
            return None
        for name in sorted(os.listdir(base)):
            if name.lower() == lang.lower() or name.lower().startswith(lang.lower()[:2] + "_"):
                if os.path.isfile(os.path.join(base, name, "fdmprinter.def.json.po")):
                    return name
        return None

    def get(self, lang):
        directory = self.directory_for(lang)
        if directory is None:
            return {}
        with self._lock:
            if directory not in self._cache:
                self._cache[directory] = parse_po(
                    os.path.join(self.resources, "i18n", directory, "fdmprinter.def.json.po"))
            return self._cache[directory]


# --- Profile overrides ----------------------------------------------------------

def _parse_number(value, kind):
    value = str(value).strip().replace(",", ".")
    number = float(value)
    if kind == "int":
        if number != int(number):
            raise ValueError(value)
        return int(number)
    return number


def sanitize_overrides(definition, overrides):
    """Keep known, editable settings; return (clean values as text, errors)."""
    clean, errors = {}, {}
    for key, value in (overrides or {}).items():
        setting = definition.settings.get(key)
        if setting is None or setting.type == "category" or value is None or str(value).strip() == "":
            continue
        kind = setting.type
        if kind in ("float", "int"):
            try:
                number = _parse_number(value, kind)
            except ValueError:
                errors[key] = "invalid"
                continue
            clean[key] = str(number)
        elif kind == "bool":
            clean[key] = "True" if str(value).strip().lower() in ("true", "1", "yes", "on") else "False"
        elif kind in ("enum", "str"):
            clean[key] = str(value)
    return clean, errors


def split_overrides(definition, flat):
    """Split overrides (key -> text) into (global layer, extruder 0 layer)."""
    global_values, extruder_values = {}, {}
    for key, value in (flat or {}).items():
        setting = definition.settings.get(key)
        if setting is not None and setting.get("settable_per_extruder"):
            extruder_values[key] = value
        else:
            global_values[key] = value
    return global_values, extruder_values


def flatten_overrides(profile_data):
    flat = dict(profile_data.get("user") or {})
    extruders = profile_data.get("user_extruders") or []
    if extruders:
        flat.update(extruders[0] or {})
    return flat


# --- Tree and rows -----------------------------------------------------------------------

def _text(translations, ctx, fallback):
    return translations.get(ctx) or fallback


def categories(definition, visibility, level, translations):
    result = []
    for root in definition.roots:
        if root.key in HIDDEN_CATEGORIES:
            continue
        count = sum(1 for _ in _walk_visible(root, visibility, level))
        if count:
            result.append({"key": root.key, "label": _text(translations, root.key + " label", root.get("label")),
                           "count": count})
    return result


def _subtree_visible(setting, visibility, level):
    if is_visible(visibility, setting.key, level):
        return True
    return any(_subtree_visible(c, visibility, level) for c in setting.children)


def _walk_visible(setting, visibility, level, depth=-1):
    """Settings (excluding categories) to display, with their depth."""
    for child in setting.children:
        if not _subtree_visible(child, visibility, level):
            continue
        yield child, depth + 1
        for item in _walk_visible(child, visibility, level, depth + 1):
            yield item


def category_keys(definition, category, visibility, level):
    root = definition.settings.get(category)
    if root is None or root.key in HIDDEN_CATEGORIES:
        return []
    return [(s.key, d) for s, d in _walk_visible(root, visibility, level)]


def search_keys(definition, query, visibility, level, translations, limit=60):
    needle = _norm(query)
    result = []
    if not needle:
        return result
    for root in definition.roots:
        if root.key in HIDDEN_CATEGORIES:
            continue
        for setting, _depth in _walk_visible(root, visibility, level):
            label = _text(translations, setting.key + " label", setting.get("label"))
            if needle in _norm(label) or needle in _norm(setting.key):
                result.append((setting.key, 0))
                if len(result) >= limit:
                    return result
    return result


def _norm(text):
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def _plain(value):
    if isinstance(value, (bool, int, str)) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return str(value)


def _stack_for(machine, setting):
    if setting.get("settable_per_extruder") and machine.extruders:
        return machine.extruders[0]
    return machine.global_stack


def row_structure(definition, key, depth, visibility, translations):
    setting = definition.settings[key]
    options = []
    for opt_key, opt_label in (setting.get("options") or {}).items():
        options.append({"key": opt_key, "label": _text(translations, "{} option {}".format(key, opt_key), opt_label)})
    return {
        "key": key, "depth": depth, "type": setting.type, "unit": setting.get("unit") or "",
        "label": _text(translations, key + " label", setting.get("label")),
        "description": _text(translations, key + " description", setting.get("description") or ""),
        "options": options, "level": level_of(visibility, key),
        "editable": setting.type in EDITABLE_TYPES,
    }


def row_values(machine, base_machine, key, definition, translations):
    """Value, base value, "enabled" state and limit messages of a setting."""
    setting = definition.settings[key]
    stack = _stack_for(machine, setting)
    messages = MESSAGES
    value = _plain(stack.get_property(key, "value"))
    out = {"value": value, "default": _plain(_stack_for(base_machine, setting).get_property(key, "value")),
           "enabled": bool(stack.get_property(key, "enabled")), "error": "", "warning": ""}
    if setting.type in ("float", "int") and isinstance(value, (int, float)) and not isinstance(value, bool):
        def limit(prop):
            try:
                return stack.get_property(key, prop)
            except Exception:
                return None

        minimum, maximum = limit("minimum_value"), limit("maximum_value")
        min_warn, max_warn = limit("minimum_value_warning"), limit("maximum_value_warning")
        out["min"], out["max"] = _plain(minimum), _plain(maximum)
        if isinstance(minimum, (int, float)) and value < minimum:
            out["error"] = messages["min"].format(_plain(minimum))
        elif isinstance(maximum, (int, float)) and value > maximum:
            out["error"] = messages["max"].format(_plain(maximum))
        elif isinstance(min_warn, (int, float)) and value < min_warn:
            out["warning"] = messages["min_w"].format(_plain(min_warn))
        elif isinstance(max_warn, (int, float)) and value > max_warn:
            out["warning"] = messages["max_w"].format(_plain(max_warn))
    return out
