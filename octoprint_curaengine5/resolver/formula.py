# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evaluation of Cura setting formulas (properties "value", "resolve"...).

Standalone reimplementation of the behavior of ``UM.Settings.SettingFunction``:
same restrictive syntax analysis (name blacklist, no slices, no
list/dict comprehensions, limited attributes), same known names,
and same rule "an evaluation error yields 0".

Formulas can come from profiles imported by the user: in addition to
Uranium's blacklist, evaluation runs with a ``__builtins__`` reduced
to the allowed functions only.
"""
import ast
import base64
import builtins
import hashlib
import logging
import math
import uuid

_logger = logging.getLogger(__name__)


class IllegalFormulaError(Exception):
    pass


KNOWN_NAMES = {
    "math", "round", "max", "ceil", "min", "sqrt", "log", "tan", "cos", "sin",
    "atan", "acos", "asin", "pi", "floor", "debug", "sum", "len", "uuid",
    "hashlib", "base64", "uuid3", "NAMESPACE_DNS", "decode", "encode",
    "b64encode", "digest", "md5", "radians", "degrees", "lower", "upper",
    "startswith", "endswith", "capitalize", "index",
    # Functions added by Cura (CuraApplication.registerOperator).
    "extruderValue", "extruderValues", "anyExtruderWithMaterial",
    "anyExtruderNrWithOrDefault", "resolveOrValue", "defaultExtruderPosition",
    "valueFromContainer", "extruderValueFromContainer",
}

ALLOWED_BUILTINS = {
    "bool", "True", "False", "None", "float", "int", "str", "sum", "pow",
    "abs", "all", "any", "round", "divmod", "hash", "len", "max", "min", "map",
}

_BLACKLIST = {
    "sys", "os", "delattr", "getattr", "dir", "open", "write", "compile",
    "import", "__import__", "__self__", "_", "__", ".", "_._", "__enter__",
    "__builtins__", "eval", "exec", "execfile", "file", "subprocess",
    "globals", "__class__", "__globals__", "hasattr", "raw_input", "input",
    "reload", "setattr", "vars", "locals", "system", "literal_eval",
    "ast.literal_eval", "ast", "lambda", "__getattribute__", "__setattr__",
    "find_module", "__pycache__", "__file__",
} | (set(dir(builtins)) - ALLOWED_BUILTINS)

_BUILTIN_NAMES = set(dir(builtins))


class _Visitor(ast.NodeVisitor):
    """Same analysis as Uranium's ``_SettingExpressionVisitor``."""

    def __init__(self):
        super().__init__()
        self.values = set()
        self.keys = set()

    def visit_Name(self, node):
        if node.id in _BLACKLIST:
            raise IllegalFormulaError(node.id)
        if node.id not in KNOWN_NAMES and node.id not in _BUILTIN_NAMES:
            self.values.add(node.id)
            self.keys.add(node.id)

    def visit_DictComp(self, node):
        raise IllegalFormulaError("Dict Comprehension is not allowed")

    def visit_ListComp(self, node):
        raise IllegalFormulaError("List Comprehension is not allowed")

    # Additions compared to Uranium (hardening): these forms are not used by
    # any Cura formula and open up escape possibilities.
    def visit_SetComp(self, node):
        raise IllegalFormulaError("Set Comprehension is not allowed")

    def visit_Lambda(self, node):
        raise IllegalFormulaError("lambda")

    def visit_NamedExpr(self, node):
        raise IllegalFormulaError("Assignment expressions are not allowed")

    def visit_Attribute(self, node):
        if node.attr not in KNOWN_NAMES:
            raise IllegalFormulaError(node.attr)
        self.visit(node.value)

    def generic_visit(self, node):
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def visit_Slice(self, node):
        raise IllegalFormulaError("Slices are not allowed")

    def visit_Subscript(self, node):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            raise IllegalFormulaError("Indexing on strings is not allowed")
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def visit_Constant(self, node):
        if not isinstance(node.value, str):
            return
        if node.value in _BLACKLIST or node.value.startswith("_"):
            raise IllegalFormulaError(node.value)
        if node.value not in KNOWN_NAMES and node.value not in _BUILTIN_NAMES:
            self.keys.add(node.value)


def _debug(value):
    _logger.debug("Setting Function: %s", value)
    return value


_SAFE_BUILTINS = {name: getattr(builtins, name) for name in ALLOWED_BUILTINS if hasattr(builtins, name)}

#: Base global namespace of formulas (equivalent of the globals() of
#: Uranium's SettingFunction module, restricted to what is useful).
BASE_GLOBALS = {
    "__builtins__": _SAFE_BUILTINS,
    "math": math,
    "uuid": uuid,
    "hashlib": hashlib,
    "base64": base64,
    "debug": _debug,
}


class Formula:
    """A compiled Cura formula (equivalent of ``SettingFunction``)."""

    __slots__ = ("code", "used_values", "used_keys", "_compiled", "valid")

    def __init__(self, code):
        self.code = str(code)
        self.used_values = frozenset()
        self.used_keys = frozenset()
        self._compiled = None
        self.valid = False
        try:
            tree = ast.parse(self.code, "eval")
            visitor = _Visitor()
            visitor.visit(tree)
            self.used_values = frozenset(visitor.values)
            self.used_keys = frozenset(visitor.keys)
            self._compiled = compile(self.code, "<formula>", "eval")
            self.valid = True
        except (SyntaxError, TypeError) as e:
            _logger.warning("Syntax error in formula (%s): %s", self.code, e)
        except IllegalFormulaError as e:
            _logger.warning("Formula rejected (%s): forbidden use of %s", self.code, e)
        except Exception as e:  # pragma: no cover - to be safe, like Uranium
            _logger.warning("Invalid formula (%s): %s", self.code, e)

    def evaluate(self, value_provider, context=None, operators=None, additional_variables=None):
        """Evaluates the formula.

        :param value_provider: stack that provides the values of the referenced settings
            (replaced by the context's root stack if there is one, as in Uranium).
        :param operators: Cura functions (extruderValue...) to expose.
        """
        if value_provider is None or not self.valid:
            return None
        if context is not None:
            value_provider = context.root_stack()
        local_variables = {}
        for name in self.used_values:
            value = value_provider.get_property(name, "value", context)
            if value is None:
                continue
            local_variables[name] = value
        if additional_variables:
            local_variables.update(additional_variables)
        global_variables = dict(BASE_GLOBALS)
        if operators:
            global_variables.update(operators)
        if context is not None:
            global_variables.update(context.context.get("override_operators", {}))
        try:
            return eval(self._compiled, global_variables, local_variables)
        except Exception as e:
            _logger.debug("Exception in formula %s: %s", self.code, e)
            return 0  # like Uranium: settings are used in calculations

    def __eq__(self, other):
        return isinstance(other, Formula) and other.code == self.code

    def __hash__(self):
        return hash(self.code)

    def __str__(self):
        return "=" + self.code

    def __repr__(self):
        return "<Formula ={}>".format(self.code)
