# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cura container stacks (global and extruders) and formula functions.

Reproduces the behavior of ``UM.Settings.ContainerStack``,
``cura.Settings.CuraContainerStack``, ``GlobalStack``, ``ExtruderStack`` and
``CuraFormulaFunctions`` from Cura 5.11, without Qt or Uranium.
"""
import logging
from collections import deque

from .containers import SLOTS, InstanceContainer
from .formula import Formula

_logger = logging.getLogger(__name__)

_VALUE_PROPERTIES = ("value", "state", "validationState")


class EvaluationContext:
    """Equivalent of ``PropertyEvaluationContext``."""

    def __init__(self, source_stack=None):
        self.stack_of_containers = deque()
        if source_stack is not None:
            self.stack_of_containers.append(source_stack)
        self.context = {}

    def root_stack(self):
        if self.stack_of_containers:
            return self.stack_of_containers[0]
        return None

    def push(self, container):
        self.stack_of_containers.append(container)

    def pop(self):
        return self.stack_of_containers.pop()


class ContainerStack:
    """Pile : user, quality_changes, intent, quality, material, variant,
    definition_changes, then the definition."""

    def __init__(self, stack_id, definition, containers=None, metadata=None):
        self.id = stack_id
        self.definition = definition
        self.metadata = dict(metadata or {})
        self.slots = {}
        for slot in SLOTS:
            self.slots[slot] = InstanceContainer("empty_" + slot, slot)
        for slot, container in (containers or {}).items():
            self.set_container(slot, container)
        self.next_stack = None
        self.machine = None  # Machine object, which carries the formula functions
        self._settable_per_extruder_cache = {}

    def set_container(self, slot, container):
        if slot not in self.slots:
            raise KeyError("Unknown layer: {}".format(slot))
        if container is None:
            container = InstanceContainer("empty_" + slot, slot)
        self.slots[slot] = container

    @property
    def containers(self):
        return [self.slots[s] for s in SLOTS] + [self.definition]

    @property
    def operators(self):
        return self.machine.operators if self.machine is not None else {}

    # --- ContainerStack (Uranium) ---------------------------------------

    def get_raw_property(self, key, property_name, context=None, use_next=True):
        containers = self.containers
        if context is not None:
            start_index = context.context.get("evaluate_from_container_index", 0)
            if start_index >= len(containers):
                return None
            containers = containers[start_index:]
        if property_name not in _VALUE_PROPERTIES:
            value = containers[-1].get_property(key, property_name, context)
            if value is not None:
                return value
        else:
            for container in containers:
                value = container.get_property(key, property_name, context)
                if value is not None:
                    return value
        if self.next_stack is not None and use_next:
            return self.next_stack.get_raw_property(key, property_name, context=context, use_next=use_next)
        return None

    def _base_get_property(self, key, property_name, context=None):
        value = self.get_raw_property(key, property_name, context=context)
        if isinstance(value, Formula):
            if context is not None:
                context.push(self)
            value = value.evaluate(self, context, self.operators)
            if context is not None:
                context.pop()
        return value

    # --- CuraContainerStack ------------------------------------------------

    def _cura_get_property(self, key, property_name, context=None):
        if property_name == "settable_per_extruder":
            try:
                return self._settable_per_extruder_cache[key]
            except KeyError:
                value = self._base_get_property(key, property_name, context)
                self._settable_per_extruder_cache[key] = value
                return value
        return self._base_get_property(key, property_name, context)

    def get_property(self, key, property_name, context=None):
        return self._cura_get_property(key, property_name, context)

    def has_user_value(self, key):
        return self.slots["user"].has_value(key) or self.slots["quality_changes"].has_value(key)

    def all_keys(self):
        keys = set(self.definition.keys())
        if self.next_stack is not None:
            keys |= self.next_stack.all_keys()
        return keys

    @property
    def material(self):
        return self.slots["material"]

    def __repr__(self):
        return "<{} {}>".format(type(self).__name__, self.id)


class GlobalStack(ContainerStack):
    def __init__(self, stack_id, definition, containers=None, metadata=None):
        super().__init__(stack_id, definition, containers, metadata)
        self.extruders = {}  # position (str) -> ExtruderStack
        self._resolving = set()

    def add_extruder(self, extruder):
        position = str(extruder.position)
        self.extruders[position] = extruder

    @property
    def extruder_list(self):
        result = [stack for _, stack in sorted(self.extruders.items(), key=lambda item: int(item[0]))]
        count = self.get_property("machine_extruder_count", "value")
        return result[:count]

    def _should_resolve(self, key, property_name, context=None):
        if property_name != "value":
            return False
        if not self.definition.get_property(key, "resolve"):
            return False
        if key in self._resolving:
            return False  # recursion guard (like GlobalStack._resolving_settings)
        if self.has_user_value(key):
            return False
        return True

    def get_property(self, key, property_name, context=None):
        if not self.definition.has(key):
            return None
        if context is not None:
            context.push(self)

        if self._should_resolve(key, property_name, context):
            self._resolving.add(key)
            try:
                resolve = self._cura_get_property(key, "resolve", context)
            finally:
                self._resolving.discard(key)
            if resolve is not None:
                return resolve

        limit_to_extruder = self._cura_get_property(key, "limit_to_extruder", context)
        if limit_to_extruder is not None:
            limit_to_extruder = str(limit_to_extruder)
        if limit_to_extruder is not None and limit_to_extruder != "-1" and limit_to_extruder in self.extruders:
            if self._cura_get_property(key, "settable_per_extruder", context):
                result = self.extruders[limit_to_extruder].get_property(key, property_name, context)
                if result is not None:
                    if context is not None:
                        context.pop()
                    return result
            else:
                _logger.debug("%s has limit_to_extruder but is not settable per extruder", key)

        result = self._cura_get_property(key, property_name, context)
        if context is not None:
            context.pop()
        return result


class ExtruderStack(ContainerStack):
    def __init__(self, stack_id, definition, position, containers=None, metadata=None, enabled=True):
        super().__init__(stack_id, definition, containers, metadata)
        self.position = int(position)
        self.enabled = enabled

    def set_next_stack(self, global_stack):
        self.next_stack = global_stack
        global_stack.add_extruder(self)

    def get_property(self, key, property_name, context=None):
        if self.next_stack is None:
            raise RuntimeError("Extruder {} has no global stack".format(self.id))
        if context is not None:
            context.push(self)

        if not self._cura_get_property(key, "settable_per_extruder", context):
            result = self.next_stack.get_property(key, property_name, context)
            if context is not None:
                context.pop()
            return result

        if context is None:
            context = EvaluationContext(self)
        if "extruder_position" not in context.context:
            context.context["extruder_position"] = self._cura_get_property(key, "limit_to_extruder", context)
        limit_to_extruder = context.context["extruder_position"]
        if limit_to_extruder is not None:
            limit_to_extruder = str(limit_to_extruder)

        if (limit_to_extruder is not None and limit_to_extruder != "-1") and str(self.position) != limit_to_extruder:
            try:
                result = self.next_stack.extruder_list[int(limit_to_extruder)].get_property(key, property_name, context)
                if result is not None:
                    context.pop()
                    return result
            except IndexError:
                pass

        result = self._cura_get_property(key, property_name, context)
        context.pop()
        return result


class FormulaFunctions:
    """Functions exposed to formulas (``CuraFormulaFunctions``)."""

    def __init__(self, machine):
        self.machine = machine

    @property
    def global_stack(self):
        return self.machine.global_stack

    def default_extruder_position(self):
        return self.machine.default_extruder_position

    def value_in_extruder(self, extruder_position, property_key, context=None):
        if extruder_position == -1:
            extruder_position = int(self.machine.default_extruder_position)
        try:
            extruder_stack = self.global_stack.extruder_list[int(extruder_position)]
        except IndexError:
            if extruder_position != 0:
                return self.value_in_extruder(0, property_key, context)
            return None
        value = extruder_stack.get_raw_property(property_key, "value", context=context)
        if isinstance(value, Formula):
            value = value.evaluate(extruder_stack, context, self.machine.operators)
        if isinstance(value, str):
            value = value.lower()
        return value

    def _active_extruders(self, context=None):
        result = []
        count = self.global_stack.get_property("machine_extruder_count", "value", context)
        for extruder in self.global_stack.extruder_list:
            if not extruder.enabled:
                continue
            if extruder.position >= count:
                continue
            result.append(extruder)
        return result

    def values_in_all_extruders(self, property_key, context=None):
        result = []
        for extruder in self._active_extruders(context):
            value = extruder.get_raw_property(property_key, "value", context=context)
            if value is None:
                continue
            if isinstance(value, Formula):
                value = value.evaluate(extruder, context, self.machine.operators)
            result.append(value)
        if not result:
            result.append(self.global_stack.get_property(property_key, "value", context))
        return result

    def any_extruder_nr_with_or_default(self, filter_key, context=None):
        for extruder in self._active_extruders(context):
            value = extruder.get_raw_property(filter_key, "value", context=context)
            if value is None or not value:
                continue
            return str(extruder.position)
        return None  # like Cura (no "default" despite the name)

    def any_extruder_with_material(self, filter_key, context=None):
        for extruder in self._active_extruders(context):
            value = extruder.material.get_property(filter_key, "value", context)
            if value is not None:
                return str(extruder.position)
        return self.default_extruder_position()

    def resolve_or_value(self, property_key, context=None):
        return self.global_stack.get_property(property_key, "value", context)

    def _default_context(self, source_stack):
        context = EvaluationContext(source_stack)
        context.context["evaluate_from_container_index"] = 1  # ignore the "user" layer
        context.context["override_operators"] = {
            "extruderValue": self.default_value_in_extruder,
            "extruderValues": self.default_values_in_all_extruders,
            "resolveOrValue": self.default_resolve_or_value,
        }
        return context

    def default_value_in_extruder(self, extruder_position, property_key):
        try:
            extruder_stack = self.global_stack.extruder_list[extruder_position]
        except (IndexError, TypeError):
            return None
        return self.value_in_extruder(extruder_position, property_key, context=self._default_context(extruder_stack))

    def default_values_in_all_extruders(self, property_key):
        return self.values_in_all_extruders(property_key, context=self._default_context(self.global_stack))

    def default_resolve_or_value(self, property_key):
        return self.resolve_or_value(property_key, context=self._default_context(self.global_stack))

    def value_from_container(self, property_key, container_index, context=None):
        context = self._default_context(self.global_stack)
        context.context["evaluate_from_container_index"] = container_index
        return self.global_stack.get_property(property_key, "value", context)

    def extruder_value_from_container(self, extruder_position, property_key, container_index, context=None):
        if extruder_position == -1:
            extruder_position = int(self.machine.default_extruder_position)
        try:
            extruder_stack = self.global_stack.extruder_list[int(extruder_position)]
        except IndexError:
            return None
        context = self._default_context(extruder_stack)
        context.context["evaluate_from_container_index"] = container_index
        return self.value_in_extruder(extruder_position, property_key, context)

    def operators(self):
        return {
            "extruderValue": self.value_in_extruder,
            "extruderValues": self.values_in_all_extruders,
            "anyExtruderWithMaterial": self.any_extruder_with_material,
            "anyExtruderNrWithOrDefault": self.any_extruder_nr_with_or_default,
            "resolveOrValue": self.resolve_or_value,
            "defaultExtruderPosition": self.default_extruder_position,
            "valueFromContainer": self.value_from_container,
            "extruderValueFromContainer": self.extruder_value_from_container,
        }
