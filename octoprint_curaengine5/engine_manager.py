# SPDX-License-Identifier: AGPL-3.0-or-later
"""Installation of a prebuilt CuraEngine (binary + Cura resources).

Nothing is downloaded automatically: installation is triggered by an
administrator from the settings page. The expected archive is produced by
the ``.github/workflows/build-curaengine.yml`` workflow:

    curaengine-<version>-linux-<arch>/
        CuraEngine
        lib/                      (shared libraries found through the engine's RPATH)
        NOTICE
        resources/{definitions,extruders,quality,intent,variants,materials}/
        LICENSES/
        VERSION

and comes with a ``SHA256SUMS`` file (``sha256sum`` format).
"""
import hashlib
import logging
import os
import platform
import shutil
import struct
import tarfile
import tempfile
import urllib.request

_logger = logging.getLogger(__name__)

ENGINE_VERSION = "5.11.0"
#: Minimum glibc of the workflow binaries (built in Debian 12 Bookworm).
MIN_GLIBC = (2, 36)
SUPPORTED_ARCHES = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64",
                    "armv7l": "armv7hf", "armv8l": "armv7hf"}
MAX_DOWNLOAD = 300 * 1024 * 1024


class InstallError(Exception):
    pass


def current_arch():
    arch = SUPPORTED_ARCHES.get(platform.machine().lower())
    # 64-bit kernel with a 32-bit userland (e.g. 32-bit Raspberry Pi OS on a Pi 4): the
    # Python interpreter, and so any binary we install, is 32-bit
    if arch == "aarch64" and struct.calcsize("P") == 4:
        arch = "armv7hf"
    return arch


def glibc_version():
    try:
        name, version = os.confstr("CS_GNU_LIBC_VERSION").split()
        return tuple(int(x) for x in version.split(".")[:2])
    except (AttributeError, ValueError, OSError):
        return None


def check_platform():
    """Return (arch, problem if any)."""
    arch = current_arch()
    if arch is None:
        return None, "Unsupported architecture {} (aarch64, x86_64 or 32-bit ARM required).".format(platform.machine())
    glibc = glibc_version()
    if glibc is not None and glibc < MIN_GLIBC:
        return arch, "glibc {}.{} is too old: {}.{} minimum (Debian 12 / OctoPi 1.1 or newer).".format(
            glibc[0], glibc[1], MIN_GLIBC[0], MIN_GLIBC[1])
    return arch, None


def asset_names(version=ENGINE_VERSION, arch=None):
    arch = arch or current_arch()
    base = "curaengine-{}-linux-{}".format(version, arch)
    return base, base + ".tar.xz"


def _download(url, destination):
    request = urllib.request.Request(url, headers={"User-Agent": "OctoPrint-CuraEngine5"})
    with urllib.request.urlopen(request, timeout=60) as response, open(destination, "wb") as out:
        total = 0
        while True:
            chunk = response.read(1024 * 256)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_DOWNLOAD:
                raise InstallError("Download too large")
            out.write(chunk)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _expected_hash(sums_text, filename):
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0].lower()
    raise InstallError("Hash of {} not found in SHA256SUMS".format(filename))


def _safe_extract(archive_path, destination):
    with tarfile.open(archive_path, "r:*") as tar:
        root = os.path.realpath(destination)
        for member in tar.getmembers():
            target = os.path.realpath(os.path.join(destination, member.name))
            if not (target == root or target.startswith(root + os.sep)):
                raise InstallError("Forbidden path in archive: {}".format(member.name))
            if member.issym() or member.islnk():
                link = os.path.realpath(os.path.join(os.path.dirname(target), member.linkname))
                if not link.startswith(root + os.sep):
                    raise InstallError("Forbidden link in archive: {}".format(member.name))
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise InstallError("Forbidden file type in archive: {}".format(member.name))
        tar.extractall(destination)


def install_from_archive(archive_path, data_folder, expected_sha256=None):
    """Install an already downloaded archive into ``data_folder``/engine and /resources."""
    if expected_sha256 and _sha256(archive_path) != expected_sha256.lower():
        raise InstallError("Incorrect SHA-256 hash: archive corrupted or modified")
    work = tempfile.mkdtemp(prefix="curaengine5-install-", dir=data_folder)
    try:
        _safe_extract(archive_path, work)
        entries = [e for e in os.listdir(work) if not e.startswith(".")]
        if len(entries) != 1:
            raise InstallError("Unexpected archive: a single root folder is expected")
        root = os.path.join(work, entries[0])
        engine = os.path.join(root, "CuraEngine")
        resources = os.path.join(root, "resources")
        if not os.path.isfile(engine) or not os.path.isfile(os.path.join(resources, "definitions", "fdmprinter.def.json")):
            raise InstallError("Incomplete archive (CuraEngine or resources/definitions missing)")
        os.chmod(engine, 0o755)
        engine_dir = os.path.join(data_folder, "engine")
        resources_dir = os.path.join(data_folder, "resources")
        for old in (engine_dir, resources_dir):
            if os.path.exists(old):
                shutil.rmtree(old)
        os.makedirs(engine_dir)
        shutil.move(engine, os.path.join(engine_dir, "CuraEngine"))
        for extra in ("LICENSES", "VERSION", "NOTICE", "lib"):
            if os.path.exists(os.path.join(root, extra)):
                shutil.move(os.path.join(root, extra), os.path.join(engine_dir, extra))
        shutil.move(resources, resources_dir)
        return os.path.join(engine_dir, "CuraEngine"), resources_dir
    finally:
        shutil.rmtree(work, ignore_errors=True)


def download_and_install(base_url, data_folder, version=ENGINE_VERSION, arch=None):
    """Download ``<base_url>/<archive>`` and ``<base_url>/SHA256SUMS``, then install.

    ``base_url`` is the address of a GitHub release's files, for example
    ``https://github.com/<account>/OctoPrint-CuraEngine5/releases/download/engine-5.11.0``.
    """
    arch_found, problem = check_platform()
    arch = arch or arch_found
    if problem:
        raise InstallError(problem)
    if not base_url.startswith("https://"):
        raise InstallError("Insecure download URL (https required)")
    _, filename = asset_names(version, arch)
    tmp = tempfile.mkdtemp(prefix="curaengine5-dl-", dir=data_folder)
    try:
        sums = os.path.join(tmp, "SHA256SUMS")
        archive = os.path.join(tmp, filename)
        _download(base_url.rstrip("/") + "/SHA256SUMS", sums)
        with open(sums, encoding="utf-8") as f:
            expected = _expected_hash(f.read(), filename)
        _logger.info("Downloading %s", filename)
        _download(base_url.rstrip("/") + "/" + filename, archive)
        return install_from_archive(archive, data_folder, expected)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
