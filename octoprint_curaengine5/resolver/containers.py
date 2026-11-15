# SPDX-License-Identifier: AGPL-3.0-or-later
"""Instance containers (*.inst.cfg files): profiles, qualities, user settings..."""
import configparser
import io
import logging

from .definitions import setting_value_from_string
from .formula import Formula

_logger = logging.getLogger(__name__)

#: Order of the layers of a Cura stack (index 0 = highest priority).
SLOTS = (
    "user",
    "quality_changes",
    "intent",
    "quality",
    "material",
    "variant",
    "definition_changes",
)


def parse_cfg(text):
    """Reads an .inst.cfg file (ConfigParser format, like Uranium)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_file(io.StringIO(text))
    general = dict(parser["general"]) if parser.has_section("general") else {}
    metadata = dict(parser["metadata"]) if parser.has_section("metadata") else {}
    values = dict(parser["values"]) if parser.has_section("values") else {}
    return general, metadata, values


class InstanceContainer:
    """A set of setting values (equivalent of ``UM.Settings.InstanceContainer``).

    Values are kept in raw form (text or formula) and then
    converted on read according to the setting type, as in Uranium.
    Keys absent from the associated definition are ignored.
    """

    def __init__(self, container_id="empty", container_type="", definition=None, values=None, metadata=None, name=None):
        self.id = container_id
        self.name = name or container_id
        self.type = container_type
        self.metadata = dict(metadata or {})
        self.definition = definition
        self._raw = {}
        self.ignored_keys = []
        for key, value in (values or {}).items():
            self.set_value(key, value)

    @classmethod
    def from_cfg(cls, text, definition, container_id=None):
        general, metadata, values = parse_cfg(text)
        return cls(
            container_id=container_id or general.get("name", "instance"),
            container_type=metadata.get("type", ""),
            definition=definition,
            values=values,
            metadata=metadata,
            name=general.get("name"),
        )

    def set_value(self, key, value):
        if self.definition is not None and not self.definition.has(key):
            self.ignored_keys.append(key)
            _logger.debug("Setting %s unknown to definition %s, ignored", key, self.definition.id)
            return
        if isinstance(value, str) and value.strip().startswith("="):
            value = Formula(value[1:])
        self._raw[key] = value

    def remove_value(self, key):
        self._raw.pop(key, None)

    def keys(self):
        return self._raw.keys()

    def raw_values(self):
        """Raw values (text or formula serialized as "=...")."""
        return {k: (str(v) if isinstance(v, Formula) else v) for k, v in self._raw.items()}

    def get_property(self, key, property_name, context=None):
        if property_name != "value":
            return None
        if key not in self._raw:
            return None
        value = self._raw[key]
        if isinstance(value, str) and self.definition is not None:
            try:
                return setting_value_from_string(self.definition.setting_type(key), value)
            except Exception:
                return value
        return value

    def has_value(self, key):
        return key in self._raw

    def is_empty(self):
        return not self._raw

    def __repr__(self):
        return "<InstanceContainer {} ({}, {} valeurs)>".format(self.id, self.type, len(self._raw))


def empty(container_type=""):
    return InstanceContainer("empty_" + container_type if container_type else "empty", container_type)
