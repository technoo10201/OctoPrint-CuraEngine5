# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compares the standalone resolver to the values computed by Uranium/Cura 5.11.

The ``tests/reference/*.reference.json`` files are produced by
``tools/uranium_reference.py`` from the ``*.scenario.json`` scenarios.
"""
import glob
import json
import math
import os

import pytest

from .conftest import REFERENCE, load_scenario

SCENARIOS = sorted(glob.glob(os.path.join(REFERENCE, "*.scenario.json")))


def _jsonable(value):
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def _same(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or (a == b and type(a) is type(b))
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def differences(ours, reference):
    diffs = []
    for key in sorted(set(ours) | set(reference)):
        if key not in ours:
            diffs.append((key, "<absent>", reference[key]))
        elif key not in reference:
            diffs.append((key, ours[key], "<absent>"))
        elif not _same(ours[key], reference[key]):
            diffs.append((key, ours[key], reference[key]))
    return diffs


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[os.path.basename(s).split(".")[0] for s in SCENARIOS])
def test_matches_uranium(scenario, loader, resources):
    reference_path = scenario.replace(".scenario.json", ".reference.json")
    with open(reference_path, encoding="utf-8") as f:
        reference = json.load(f)
    machine = load_scenario(scenario, loader, resources)
    ours = _jsonable(machine.resolve_all())

    report = []
    diffs = differences(ours["global"], reference["global"])
    report += ["global    %-45s ours=%r  uranium=%r" % d for d in diffs]
    for i, (ext_ours, ext_ref) in enumerate(zip(ours["extruders"], reference["extruders"])):
        report += ["extruder%d %-44s ours=%r  uranium=%r" % ((i,) + d) for d in differences(ext_ours, ext_ref)]
    assert len(ours["extruders"]) == len(reference["extruders"])
    assert not report, "%d difference(s) from Uranium:\n%s" % (len(report), "\n".join(report))
