# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assembly of a machine (global stack + extruders) and full resolution."""
import logging

from .containers import SLOTS, InstanceContainer
from .stacks import ExtruderStack, FormulaFunctions, GlobalStack

_logger = logging.getLogger(__name__)


class Machine:
    """A configured printer: definition, extruders and setting layers.

    :param loader: ``DefinitionLoader``
    :param definition_id: machine definition identifier (e.g. ``eryone_thinker``)
    :param global_containers: {layer: InstanceContainer or dict of raw values}
    :param extruder_containers: list (by position) of {layer: ...}
    """

    def __init__(self, loader, definition_id, global_containers=None, extruder_containers=None, extruders_enabled=None):
        self.loader = loader
        self.definition = loader.load(definition_id)
        self.global_stack = GlobalStack("global", self.definition)
        self.global_stack.machine = self
        for slot, container in (global_containers or {}).items():
            self.global_stack.set_container(slot, self._as_container(container, slot, "global"))

        trains = self.definition.metadata.get("machine_extruder_trains", {}) or {"0": "fdmextruder"}
        extruder_containers = list(extruder_containers or [])
        for position in sorted(trains, key=int):
            ext_def = loader.load(trains[position])
            enabled = True
            if extruders_enabled is not None and int(position) < len(extruders_enabled):
                enabled = bool(extruders_enabled[int(position)])
            stack = ExtruderStack("extruder_" + position, ext_def, int(position), enabled=enabled)
            stack.machine = self
            if int(position) < len(extruder_containers):
                for slot, container in (extruder_containers[int(position)] or {}).items():
                    stack.set_container(slot, self._as_container(container, slot, "extruder_" + position))
            stack.set_next_stack(self.global_stack)

        self.functions = FormulaFunctions(self)
        self.operators = self.functions.operators()

    def _as_container(self, container, slot, owner):
        if container is None or isinstance(container, InstanceContainer):
            return container
        # The instance containers of a machine use the machine's definition,
        # including for the extruders (as in Cura).
        return InstanceContainer("{}_{}".format(owner, slot), slot, self.definition, values=container)

    @property
    def extruders(self):
        return self.global_stack.extruder_list

    @property
    def default_extruder_position(self):
        for extruder in self.global_stack.extruder_list:
            if extruder.enabled:
                return str(extruder.position)
        return "0"

    def _dump(self, stack):
        result = {}
        for key in sorted(stack.all_keys()):
            if stack.get_property(key, "type") == "category":
                continue
            result[key] = stack.get_property(key, "value")
        return result

    def resolve_global(self):
        return self._dump(self.global_stack)

    def resolve_extruder(self, position):
        return self._dump(self.global_stack.extruder_list[position])

    def resolve_all(self):
        return {
            "global": self.resolve_global(),
            "extruders": [self._dump(stack) for stack in self.global_stack.extruder_list],
        }

    def get(self, key, extruder=None):
        stack = self.global_stack if extruder is None else self.global_stack.extruder_list[extruder]
        return stack.get_property(key, "value")


SLOT_NAMES = SLOTS
