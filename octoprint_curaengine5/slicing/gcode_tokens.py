# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replacement of the {...} tokens in the start/end G-code (port of
``GcodeStartEndFormatter`` from Cura 5.11's StartSliceJob).

Handles {setting}, {setting, extruder_nr}, expressions and the
{if ...} / {elif ...} / {else} / {endif} blocks.
"""
import logging
import re

from ..resolver.formula import Formula

_logger = logging.getLogger(__name__)

_INSTRUCTION_RE = re.compile(
    r"{(?P<condition>if|else|elif|endif)?\s*(?P<expression>[^{}]*?)\s*(?:,\s*(?P<extruder_nr_expr>[^{}]*))?\s*}(?P<end_of_line>\n?)"
)

#: Variables only known after slicing: left as they are.
POST_SLICE_VARIABLES = ["filament_cost", "print_time", "filament_amount", "filament_weight", "jobname"]

_OUTSIDE, _FALSE, _TRUE, _DONE = 1, 2, 3, 4
_SKIP, _EVALUATE, _EVALUATE_AND_WRITE = 1, 2, 3


class GcodeTokenFormatter:
    def __init__(self, machine, all_extruder_settings, default_extruder_nr=-1):
        """
        :param machine: ``resolver.Machine``
        :param all_extruder_settings: {"-1": global tokens, "0": extruder 0 tokens, ...}
        """
        self._machine = machine
        self._all = all_extruder_settings
        self._default_extruder_nr = default_extruder_nr
        self._state = _OUTSIDE

    def format(self, text):
        remaining = text
        result = ""
        self._state = _OUTSIDE
        while remaining:
            match = _INSTRUCTION_RE.search(remaining)
            if match is None:
                result += self._statement(remaining)
                break
            start, end = match.span()
            if start > 0:
                result += self._statement(remaining[:start])
            result += self._code(match)
            remaining = remaining[end:]
        return result

    def _statement(self, statement):
        return statement if self._state in (_OUTSIDE, _TRUE) else ""

    def _stack_for(self, extruder_nr):
        if extruder_nr == "-1":
            return self._machine.global_stack
        try:
            return self._machine.global_stack.extruder_list[int(extruder_nr)]
        except (IndexError, ValueError):
            return self._machine.global_stack

    def _code(self, match):
        condition = match.group("condition")
        expression = match.group("expression")
        extruder_nr_expr = match.group("extruder_nr_expr")
        end_of_line = match.group("end_of_line")

        if expression in POST_SLICE_VARIABLES:
            return "{" + expression + "}"

        extruder_nr = str(self._default_extruder_nr)
        instruction = _SKIP
        if condition is None:
            if self._state in (_OUTSIDE, _TRUE):
                instruction = _EVALUATE_AND_WRITE
        else:
            if condition == "if":
                if self._state != _OUTSIDE:
                    raise SyntaxError("Nested conditions are not supported")
            elif self._state == _OUTSIDE:
                raise SyntaxError("Condition should start with an 'if' statement")
            if condition == "if":
                instruction = _EVALUATE
            else:
                if self._state == _TRUE:
                    self._state = _DONE
                if condition == "elif":
                    instruction = _EVALUATE if self._state == _FALSE else _SKIP
                elif condition == "else":
                    instruction = _SKIP
                    if self._state == _FALSE:
                        self._state = _TRUE
                elif condition == "endif":
                    instruction = _SKIP
                    self._state = _OUTSIDE

        operators = self._machine.operators
        if instruction >= _EVALUATE and extruder_nr_expr is not None:
            fn = Formula(extruder_nr_expr)
            extruder_nr = str(fn.evaluate(self._machine.global_stack, operators=operators,
                                          additional_variables=self._all["-1"]))

        if instruction >= _EVALUATE:
            if extruder_nr in self._all:
                variables = dict(self._all[extruder_nr])
            else:
                _logger.warning("Extruder %s does not exist: using global settings", extruder_nr)
                variables = dict(self._all["-1"])
            stack = self._stack_for(extruder_nr)
            value = Formula(expression).evaluate(stack, operators=operators, additional_variables=variables)
            if instruction == _EVALUATE:
                self._state = _TRUE if value else _FALSE
                return ""
            value_str = str(value)
            if end_of_line is not None:
                value_str += end_of_line
            return value_str
        return ""


def expand(machine, all_extruder_settings, text, default_extruder_nr=-1):
    try:
        return str(GcodeTokenFormatter(machine, all_extruder_settings, default_extruder_nr).format(text))
    except Exception:
        _logger.exception("Token replacement failed in the start/end G-code")
        return str(text)
