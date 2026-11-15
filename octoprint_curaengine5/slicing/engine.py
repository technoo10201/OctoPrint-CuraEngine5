# SPDX-License-Identifier: AGPL-3.0-or-later
"""Running CuraEngine 5.x on the command line."""
import logging
import math
import os
import platform
import re
import subprocess
import threading
import time

_logger = logging.getLogger(__name__)

#: Weight of the engine steps in the overall progress.
_STAGES = [("slice", 0.05), ("layerparts", 0.05), ("inset+skin", 0.40), ("support", 0.10), ("export", 0.40)]
_STAGE_START = {}
_acc = 0.0
for _name, _weight in _STAGES:
    _STAGE_START[_name] = (_acc, _weight)
    _acc += _weight

_LAYERED_STAGES = ("inset+skin", "export")
_LAYER_RE = re.compile(r"(Processing insets for layer|Processing skins and infill layer|GcodeWriter processing layer) (\d+) of (\d+)")
_DONE_RE = re.compile(r"Progress: (\S+) accomplished")
_HEADER_START = "Gcode header after slicing:"
_LOG_LINE_RE = re.compile(r"^\[\d{4}-\d\d-\d\d [\d:.]+\] \[\w+\]")


class SlicingCancelled(Exception):
    pass


class EngineError(Exception):
    def __init__(self, message, log_tail=None):
        super().__init__(message)
        self.log_tail = log_tail or []


class EngineBinary:
    """How to launch CuraEngine.

    The UltiMaker AppImage binary has a relative ELF interpreter: it must
    be launched through the dynamic loader with its libraries.
    """

    def __init__(self, path, library_paths=None, loader=None):
        self.path = path
        self.library_paths = [p for p in (library_paths or []) if p]
        self.loader = loader

    @classmethod
    def detect(cls, path):
        """Builds the appropriate command: standalone binary, or binary extracted
        from an AppImage (folder that also contains runtime/compat)."""
        directory = os.path.dirname(os.path.abspath(path))
        compat = os.path.join(directory, "runtime", "compat")
        if os.path.isdir(compat) and os.path.exists(os.path.join(directory, "AppRun")):
            machine = platform.machine()
            loader = {
                "x86_64": "/lib64/ld-linux-x86-64.so.2",
                "aarch64": "/lib/ld-linux-aarch64.so.1",
            }.get(machine)
            if loader and os.path.exists(loader):
                return cls(path, [directory, compat], loader)
        return cls(path)

    def command(self):
        if self.loader:
            return [self.loader, "--library-path", ":".join(self.library_paths), self.path]
        return [self.path]

    def version(self, timeout=20):
        try:
            out = subprocess.run(self.command() + ["help"], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as e:
            _logger.debug("CuraEngine unusable: %s", e)
            return None
        match = re.search(r"version\s+(\d+\.\d+\.\d+\S*)", out.stdout + out.stderr)
        return match.group(1) if match else None


def build_command(binary, fdmprinter_json, global_settings, extruder_settings, model_path, output_path,
                  mesh_settings=None, threads=None):
    cmd = binary.command() + ["slice", "-v", "-p"]
    if threads:
        cmd.append("-m%d" % int(threads))
    cmd += ["-j", fdmprinter_json]
    for key, value in global_settings.items():
        cmd += ["-s", "{}={}".format(key, value)]
    for index, settings in enumerate(extruder_settings):
        cmd.append("-e%d" % index)
        for key, value in settings.items():
            cmd += ["-s", "{}={}".format(key, value)]
    cmd += ["-l", model_path]
    for key, value in (mesh_settings or {"extruder_nr": "0"}).items():
        cmd += ["-s", "{}={}".format(key, value)]
    cmd += ["-o", output_path]
    return cmd


class ProgressParser:
    def __init__(self):
        self.progress = 0.0
        self.header_lines = None
        self._in_header = False
        self.log_tail = []

    def feed(self, line):
        """Parses a line; returns the progress (0..1) if it changed."""
        stripped = line.rstrip("\n")
        self.log_tail.append(stripped)
        if len(self.log_tail) > 40:
            del self.log_tail[0]
        if self._in_header:
            if _LOG_LINE_RE.match(stripped):
                self._in_header = False
            else:
                self.header_lines.append(stripped)
                return None
        if _HEADER_START in stripped:
            self._in_header = True
            self.header_lines = [stripped.split(_HEADER_START, 1)[1].strip()]
            return None
        new = None
        match = _LAYER_RE.search(stripped)
        if match:
            kind, n, total = match.group(1), int(match.group(2)), max(int(match.group(3)), 1)
            stage = "export" if kind.startswith("GcodeWriter") else "inset+skin"
            start, weight = _STAGE_START[stage]
            if kind.startswith("Processing insets"):
                fraction = n / total * 0.5
            elif kind.startswith("Processing skins"):
                fraction = 0.5 + n / total * 0.5
            else:
                fraction = n / total
            new = start + weight * min(fraction, 1.0)
        else:
            match = _DONE_RE.search(stripped)
            # "inset+skin" and "export" report their end at the start of processing (multi-threaded):
            # their progress comes from the "layer n of N" lines.
            if match and match.group(1) in _STAGE_START and match.group(1) not in _LAYERED_STAGES:
                start, weight = _STAGE_START[match.group(1)]
                new = start + weight
            elif match and match.group(1) == "process":
                new = 1.0
        if new is not None and new > self.progress:
            self.progress = new
            return new
        return None

    @property
    def final_header(self):
        if not self.header_lines:
            return None
        lines = [l for l in self.header_lines if l != ""]
        return lines


def run_engine(cmd, on_progress=None, cancel_event=None, timeout=None, cwd=None):
    """Runs the engine and follows its output. Returns the ``ProgressParser``."""
    parser = ProgressParser()
    _logger.info("Launching CuraEngine (%d arguments)", len(cmd))
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding="utf-8", errors="replace", cwd=cwd, bufsize=1)
    killer_done = threading.Event()

    def watch():
        start = time.monotonic()
        while not killer_done.wait(0.25):
            if cancel_event is not None and cancel_event.is_set():
                break
            if timeout and time.monotonic() - start > timeout:
                break
        else:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()

    watcher = threading.Thread(target=watch, name="curaengine5-watch", daemon=True)
    watcher.start()
    try:
        for line in process.stdout:
            new = parser.feed(line)
            if new is not None and on_progress is not None:
                on_progress(new)
        returncode = process.wait()
    finally:
        killer_done.set()
        watcher.join(1)
    if cancel_event is not None and cancel_event.is_set():
        raise SlicingCancelled()
    if returncode != 0:
        raise EngineError("CuraEngine stopped with code {}".format(returncode), parser.log_tail)
    return parser


def _iso_duration(seconds):
    seconds = int(seconds)
    return "%02d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


def finalize_gcode(path, header_lines, job_name="", material_densities=None, material_diameters=None,
                   append_lines=None):
    """Replaces the provisional header with the final header and the post-slicing variables.

    Returns an analysis dict (estimated time, filament per extruder in mm).
    """
    info = {"print_time": None, "filament_m": []}
    for line in header_lines or []:
        if line.startswith(";TIME:"):
            try:
                info["print_time"] = float(line[6:])
            except ValueError:
                pass
        elif line.startswith(";Filament used:"):
            for part in line.split(":", 1)[1].split(","):
                part = part.strip().rstrip("m")
                try:
                    info["filament_m"].append(float(part))
                except ValueError:
                    pass

    lengths = [round(l, 2) for l in info["filament_m"]]
    weights = []
    for i, length in enumerate(info["filament_m"]):
        density = (material_densities or [None])[i] if i < len(material_densities or []) else None
        diameter = (material_diameters or [None])[i] if i < len(material_diameters or []) else None
        if density and diameter:
            volume_cm3 = length * 1000 * math.pi * (float(diameter) / 2) ** 2 / 1000
            weights.append(round(volume_cm3 * float(density), 2))
        else:
            weights.append(0.0)
    replacements = {
        "{print_time}": _iso_duration(info["print_time"]) if info["print_time"] is not None else "{print_time}",
        "{filament_amount}": str(lengths),
        "{filament_weight}": str(weights),
        "{filament_cost}": str([0.0 for _ in lengths]),
        "{jobname}": job_name,
    }

    tmp = path + ".tmp"
    with open(path, encoding="utf-8", errors="surrogateescape") as src, \
            open(tmp, "w", encoding="utf-8", errors="surrogateescape") as dst:
        first = src.readline()
        if header_lines and first.startswith(";FLAVOR:"):
            # provisional header: up to and including the first blank line
            for line in src:
                if line.strip() == "":
                    break
            for h in header_lines:
                dst.write(h + "\n")
            dst.write("\n")
        else:
            src.seek(0)
        for line in src:
            if "{" in line:
                for key, value in replacements.items():
                    line = line.replace(key, value)
            dst.write(line)
        for extra in append_lines or []:
            dst.write(extra)
    os.replace(tmp, path)
    return info
