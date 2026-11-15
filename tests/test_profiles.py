# SPDX-License-Identifier: AGPL-3.0-or-later
import io
import zipfile

import pytest

from octoprint_curaengine5.resolver.profiles import (
    ProfileError,
    read_curaprofile,
    read_gcode_profile,
    unescape_gcode_comment,
    write_gcode_profile,
)

from .conftest import DATA
import os


def test_curaprofile_abs():
    profile = read_curaprofile(os.path.join(DATA, "eryone_thinker_esun_abs.curaprofile"))
    assert profile.name == "Eryone Thinker - ABS"
    assert profile.definition == "eryone_thinker"
    assert profile.quality_type == "normal"
    assert profile.setting_version == 25
    assert profile.warnings == []
    assert profile.global_values["material_bed_temperature"] == "90"
    assert profile.global_values["support_enable"] == "True"
    assert len(profile.extruder_values) == 1
    ext = profile.extruder_values[0]
    assert ext["material_print_temperature"] == "240"
    assert ext["retraction_amount"] == "7"
    # formula kept as is
    assert ext["support_xy_distance_overhang"] == "=machine_nozzle_size / 2"


@pytest.mark.parametrize("name", ["abs", "petg", "pla"])
def test_all_curaprofiles(name):
    profile = read_curaprofile(os.path.join(DATA, "eryone_thinker_esun_%s.curaprofile" % name))
    assert profile.global_values and profile.extruder_values[0]


def test_single_inst_cfg_is_split(loader):
    with open(os.path.join(DATA, "abs_global_qc.inst.cfg"), "rb") as f:
        data = f.read()
    profile = read_curaprofile(data)
    assert profile.extruder_values == []
    profile.global_values["speed_print"] = "33"
    profile.split_single(loader.load("eryone_thinker"))
    assert profile.extruder_values[0]["speed_print"] == "33"
    assert "speed_print" not in profile.global_values
    assert "adhesion_type" in profile.global_values  # not settable per extruder


def test_curaprofile_extruder_order():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for pos in ("1", "0"):
            z.writestr("ext" + pos, "[general]\nversion = 4\nname = X\ndefinition = custom\n\n[metadata]\n"
                       "type = quality_changes\nquality_type = draft\nposition = %s\nsetting_version = 25\n\n"
                       "[values]\nspeed_print = 1%s\n" % (pos, pos))
        z.writestr("global", "[general]\nversion = 4\nname = X\ndefinition = custom\n\n[metadata]\n"
                   "type = quality_changes\nquality_type = draft\nsetting_version = 25\n\n[values]\nlayer_height = 0.3\n")
    profile = read_curaprofile(buf.getvalue())
    assert profile.global_values == {"layer_height": "0.3"}
    assert [e["speed_print"] for e in profile.extruder_values] == ["10", "11"]


def test_gcode_profile_from_cura_5_11():
    profile = read_gcode_profile(os.path.join(DATA, "cura511_eryone_settings.gcode"))
    assert profile.name == "Eryone Thinker - ABS"
    assert profile.definition == "eryone_thinker"
    assert profile.quality_type == "normal"
    assert profile.global_values["layer_height"] == "0.2"
    assert len(profile.extruder_values) == 1
    assert profile.extruder_values[0]["material_print_temperature"] == "240"


def test_gcode_roundtrip_with_multiline_and_backslash():
    profile = read_curaprofile(os.path.join(DATA, "eryone_thinker_esun_abs.curaprofile"))
    profile.global_values["machine_start_gcode"] = "G28 ;home\nM117 C:\\\\test"
    block = write_gcode_profile(profile)
    assert all(len(line) <= 80 for line in block.splitlines())
    back = read_gcode_profile(io.StringIO(block))
    assert back.global_values == profile.global_values
    assert back.extruder_values == profile.extruder_values


def test_unescape():
    assert unescape_gcode_comment("a\\\\nb\\nc") == "a\\nb\nc"


def test_gcode_without_profile():
    with pytest.raises(ProfileError):
        read_gcode_profile(io.StringIO("G28\nG1 X0\n"))
