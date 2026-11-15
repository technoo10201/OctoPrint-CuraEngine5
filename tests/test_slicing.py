# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import re
import threading

import pytest

from octoprint_curaengine5.resolver.containers import parse_cfg
from octoprint_curaengine5.resolver.library import CuraLibrary
from octoprint_curaengine5.resolver.profiles import read_curaprofile
from octoprint_curaengine5.slicing import engine, stl
from octoprint_curaengine5.slicing.engine_settings import build_engine_settings
from octoprint_curaengine5.slicing.job import merge_machine_config, slice_model

from .conftest import DATA, cura_appdir


@pytest.fixture(scope="module")
def library(resources):
    return CuraLibrary(resources)


@pytest.fixture(scope="module")
def binary():
    path = os.environ.get("CURAENGINE5_ENGINE", os.path.join(cura_appdir(), "CuraEngine"))
    if not os.path.exists(path):
        pytest.skip("Binaire CuraEngine introuvable (CURAENGINE5_ENGINE)")
    b = engine.EngineBinary.detect(path)
    if not (b.version() or "").startswith("5."):
        pytest.skip("CuraEngine 5.x cannot be run here")
    return b


def eryone_config():
    with open(os.path.join(DATA, "eryone_thinker_definition_changes.inst.cfg"), encoding="utf-8") as f:
        dc = parse_cfg(f.read())[2]
    return {"definition": "eryone_thinker", "definition_changes": dc, "extruders": [{"material": "generic_abs"}]}


def abs_profile():
    p = read_curaprofile(os.path.join(DATA, "eryone_thinker_esun_abs.curaprofile"))
    return {"quality_type": p.quality_type, "global": p.global_values, "extruders": p.extruder_values}


# --- Settings sent to the engine ---------------------------------------------

def test_engine_settings_start_gcode_tokens(library):
    machine = library.build_machine(eryone_config(), abs_profile())
    g, e = build_engine_settings(machine)
    assert "M140 S90 ;" in g["machine_start_gcode"]
    assert "M104 S240 ;Heat hotend" in g["machine_start_gcode"]
    assert "{" not in g["machine_start_gcode"]
    assert "G1 X0 Y300 ;Present print" in g["machine_end_gcode"]
    # the temperature tokens are present: no preheating added by the engine
    assert g["material_bed_temp_prepend"] == "False"
    assert g["material_print_temp_prepend"] == "False"
    assert g["machine_gcode_flavor"] == "RepRap (Marlin/Sprinter)"
    assert e[0]["material_print_temperature"] == "240"
    assert e[0]["material_type"] == "ABS"
    assert "machine_width" not in e[0]  # not settable per extruder
    assert len(e) == 1


def test_engine_settings_conditionals(library):
    config = eryone_config()
    config["definition_changes"]["machine_start_gcode"] = (
        "{if material_type == 'ABS'}M117 ABS {material_print_temperature, 0}\n{elif material_type == 'PLA'}M117 PLA\n"
        "{else}M117 autre\n{endif}G28 ;{print_time}\n")
    machine = library.build_machine(config, abs_profile())
    g, _ = build_engine_settings(machine)
    # As in Cura 5.11, a post-slicing variable swallows the newline that follows it.
    assert g["machine_start_gcode"] == "M117 ABS 240\nG28 ;{print_time}"


def test_prepend_when_no_temperature_tokens(library):
    config = eryone_config()
    config["definition_changes"]["machine_start_gcode"] = "G28 ; {material_print_temperature} en commentaire\n"
    g, _ = build_engine_settings(library.build_machine(config, abs_profile()))
    assert g["material_print_temp_prepend"] == "True"
    assert g["material_bed_temp_prepend"] == "True"


def test_build_command_layout():
    b = engine.EngineBinary("/opt/CuraEngine")
    cmd = engine.build_command(b, "/r/fdmprinter.def.json", {"a": "1"}, [{"b": "2"}], "/t/m.stl", "/t/o.gcode")
    assert cmd == ["/opt/CuraEngine", "slice", "-v", "-p", "-j", "/r/fdmprinter.def.json", "-s", "a=1",
                   "-e0", "-s", "b=2", "-l", "/t/m.stl", "-s", "extruder_nr=0", "-o", "/t/o.gcode"]


def test_appimage_binary_uses_loader(tmp_path):
    (tmp_path / "runtime" / "compat").mkdir(parents=True)
    (tmp_path / "AppRun").write_text("")
    (tmp_path / "CuraEngine").write_text("")
    b = engine.EngineBinary.detect(str(tmp_path / "CuraEngine"))
    if b.loader is None:
        pytest.skip("dynamic loader not found on this architecture")
    assert b.command()[1:3] == ["--library-path", "%s:%s" % (tmp_path, tmp_path / "runtime" / "compat")]


def test_progress_parser():
    p = engine.ProgressParser()
    lines = [
        "[2026-11-15 12:41:32.597] [info] Progress: slice accomplished in 0.000s",
        "[2026-11-15 12:41:32.598] [info] Progress: layerparts accomplished in 0.001s",
        "[2026-11-15 12:41:32.598] [debug] Processing insets for layer 50 of 100",
        "[2026-11-15 12:41:32.598] [debug] Processing skins and infill layer 100 of 100",
        "[2026-11-15 12:41:32.598] [debug] GcodeWriter processing layer 50 of 100",
        "[2026-11-15 12:41:32.622] [info] Gcode header after slicing: ;FLAVOR:Marlin",
        ";TIME:1649",
        ";Filament used: 2.00661m",
        "",
        "[2026-11-15 12:41:32.622] [info] Print time (s): 1649",
        "[2026-11-15 12:41:32.622] [info] Progress: process accomplished in 0.014s",
    ]
    values = [v for v in (p.feed(l + "\n") for l in lines) if v is not None]
    assert values == sorted(values)
    assert values[-1] == 1.0
    assert p.final_header == [";FLAVOR:Marlin", ";TIME:1649", ";Filament used: 2.00661m"]


def test_finalize_gcode(tmp_path):
    path = tmp_path / "o.gcode"
    path.write_text(";FLAVOR:Marlin\n;TIME:6666\n;Filament used: 0m\n\n;Generated with Cura_SteamEngine 5.11.0\n"
                    "M117 {print_time} {filament_amount} {jobname}\nG28\n")
    info = engine.finalize_gcode(str(path), [";FLAVOR:Marlin", ";TIME:3725", ";Filament used: 2.00661m"],
                                 job_name="cube", material_densities=["1.1"], material_diameters=[1.75])
    text = path.read_text()
    assert text.startswith(";FLAVOR:Marlin\n;TIME:3725\n;Filament used: 2.00661m\n\n;Generated with")
    assert "M117 01:02:05 [2.01] cube" in text
    assert info == {"print_time": 3725.0, "filament_m": [2.00661]}


def test_stl_prepare_centers_and_drops(tmp_path):
    src, dst = tmp_path / "a.stl", tmp_path / "b.stl"
    stl.write_box(str(src), 10, 20, 30)
    # shift the box to check the recentering
    verts = [(x + 100, y - 50, z + 7) for x, y, z in stl.read_triangles(str(src))]
    with open(src, "w") as f:
        f.write("solid t\n")
        for i in range(0, len(verts), 3):
            f.write(" facet normal 0 0 0\n  outer loop\n")
            for v in verts[i:i + 3]:
                f.write("   vertex %f %f %f\n" % v)
            f.write("  endloop\n endfacet\n")
        f.write("endsolid t\n")
    size, count = stl.prepare_for_engine(str(src), str(dst), offset_x=5)
    assert count == 12 and size == pytest.approx((10, 20, 30))
    (minx, miny, minz), (maxx, maxy, maxz) = stl.bounding_box(stl.read_triangles(str(dst)))
    assert (minx, maxx, miny, maxy, minz, maxz) == pytest.approx((0, 10, -10, 10, 0, 30))


def test_printer_profile_merge():
    pp = {"volume": {"width": 250, "depth": 210, "height": 200, "origin": "lowerleft", "formFactor": "rectangular"},
          "heatedBed": True, "extruder": {"count": 1, "nozzleDiameter": 0.6, "offsets": [[0, 0]]}}
    custom = merge_machine_config({"definition": "custom", "definition_changes": {"machine_height": "180"}}, pp)
    assert custom["definition_changes"]["machine_width"] == "250"
    assert custom["definition_changes"]["machine_height"] == "180"  # explicit setting takes priority
    assert custom["extruders"][0]["definition_changes"]["machine_nozzle_size"] == "0.6"
    eryone = merge_machine_config({"definition": "eryone_thinker"}, pp)
    assert eryone["definition_changes"] == {}  # auto mode: the Cura definition prevails


# --- Bout en bout ------------------------------------------------------------

def test_slice_cube_end_to_end(library, binary, tmp_path):
    model = tmp_path / "cube.stl"
    stl.write_box(str(model), 20, 20, 20)
    out = tmp_path / "cube.gcode"
    progress = []
    result = slice_model(library, binary, eryone_config(), abs_profile(), str(model), str(out),
                         position={"x": 100, "y": 120}, on_progress=progress.append)
    text = out.read_text(encoding="utf-8")
    layers = re.findall(r"^;LAYER:(\d+)$", text, re.M)
    assert len(layers) >= 90  # 20 mm en couches de 0,2 mm (+ couche initiale)
    assert text.startswith(";FLAVOR:Marlin\n;TIME:")
    assert ";TIME:6666" not in text and ";Filament used: 0m" not in text
    assert "M190 S90" in text and "M109 S240" in text
    assert result.analysis["estimatedPrintTime"] > 0
    assert result.analysis["filament"]["tool0"]["length"] > 0
    assert progress[0] == 0.0 and progress[-1] == 1.0 and progress == sorted(progress)
    # the cube is centered on the requested position (100, 120)
    xs = [float(m) for m in re.findall(r"^G1 .*X([\d.]+) Y[\d.]+ E", text, re.M)]
    ys = [float(m) for m in re.findall(r"^G1 .*X[\d.]+ Y([\d.]+) E", text, re.M)]
    layer10 = text.split(";LAYER:10\n", 1)[1].split(";LAYER:11\n", 1)[0]
    lx = [float(v) for v in re.findall(r"^G1 .*X([\d.]+) Y[\d.]+ E", layer10, re.M)]
    ly = [float(v) for v in re.findall(r"^G1 .*X[\d.]+ Y([\d.]+) E", layer10, re.M)]
    assert xs and ys
    assert (min(lx) + max(lx)) / 2 == pytest.approx(100, abs=0.5)
    assert (min(ly) + max(ly)) / 2 == pytest.approx(120, abs=0.5)


def test_slice_cancel(library, binary, tmp_path):
    model = tmp_path / "cube.stl"
    stl.write_box(str(model), 20, 20, 20)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(engine.SlicingCancelled):
        slice_model(library, binary, eryone_config(), abs_profile(), str(model), str(tmp_path / "x.gcode"),
                    cancel_event=cancel)
    assert not (tmp_path / "x.gcode").exists()


def test_engine_error_reported(library, binary, tmp_path):
    bad = tmp_path / "bad.stl"
    bad.write_text("solid x\nendsolid x\n")
    with pytest.raises(stl.StlError):
        slice_model(library, binary, eryone_config(), abs_profile(), str(bad), str(tmp_path / "x.gcode"))


def test_transform_model_rotation_and_scale(tmp_path):
    from octoprint_curaengine5.slicing import stl

    src, dst = str(tmp_path / "a.stl"), str(tmp_path / "b.stl")
    stl.write_box(src, 10, 20, 30)
    cx, cy = stl.transform_model(src, dst, rotation=(90, 0, 0), scale=(200, 100, 100))
    box = stl.bounding_box(stl.read_triangles(dst))
    size = [hi - lo for lo, hi in zip(*box)]
    # X scale x2 then 90 degree rotation around X: Y and Z swapped
    assert [round(v, 3) for v in size] == [20.0, 30.0, 20.0]
    assert abs(cx) < 1e-3 and abs(cy) < 1e-3
    stl.transform_model(src, dst, rotation=(0, 0, 90))
    size = [hi - lo for lo, hi in zip(*stl.bounding_box(stl.read_triangles(dst)))]
    assert [round(v, 3) for v in size] == [20.0, 10.0, 30.0]
