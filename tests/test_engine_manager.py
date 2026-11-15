# SPDX-License-Identifier: AGPL-3.0-or-later
import hashlib
import io
import os
import tarfile

import pytest

from octoprint_curaengine5 import engine_manager


def _make_archive(path, extra_member=None):
    with tarfile.open(path, "w:xz") as tar:
        def add(name, data=b"", mode=0o644):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tar.addfile(info, io.BytesIO(data))
        root = "curaengine-5.11.0-linux-aarch64/"
        add(root + "CuraEngine", b"#!/bin/sh\necho Cura_SteamEngine version 5.11.0\n", 0o755)
        add(root + "resources/definitions/fdmprinter.def.json", b"{}")
        add(root + "VERSION", b"5.11.0\n")
        add(root + "LICENSES/CuraEngine-AGPL-3.0.txt", b"...")
        if extra_member:
            add(extra_member, b"x")
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def test_install_from_archive(tmp_path):
    archive = str(tmp_path / "a.tar.xz")
    digest = _make_archive(archive)
    data = tmp_path / "data"
    data.mkdir()
    engine, resources = engine_manager.install_from_archive(archive, str(data), digest)
    assert engine == str(data / "engine" / "CuraEngine")
    assert os.access(engine, os.X_OK)
    assert os.path.isfile(os.path.join(resources, "definitions", "fdmprinter.def.json"))
    assert (data / "engine" / "VERSION").read_text() == "5.11.0\n"
    # reinstall: replaces cleanly
    engine_manager.install_from_archive(archive, str(data), digest)
    assert sorted(os.listdir(data)) == ["engine", "resources"]


def test_bad_hash_rejected(tmp_path):
    archive = str(tmp_path / "a.tar.xz")
    _make_archive(archive)
    with pytest.raises(engine_manager.InstallError):
        engine_manager.install_from_archive(archive, str(tmp_path), "0" * 64)


def test_path_traversal_rejected(tmp_path):
    archive = str(tmp_path / "a.tar.xz")
    digest = _make_archive(archive, extra_member="../evil")
    data = tmp_path / "data"
    data.mkdir()
    with pytest.raises(engine_manager.InstallError):
        engine_manager.install_from_archive(archive, str(data), digest)
    assert not (tmp_path / "evil").exists()


def test_expected_hash_lookup():
    sums = "abc123  curaengine-5.11.0-linux-aarch64.tar.xz\ndef456 *curaengine-5.11.0-linux-x86_64.tar.xz\n"
    assert engine_manager._expected_hash(sums, "curaengine-5.11.0-linux-x86_64.tar.xz") == "def456"
    with pytest.raises(engine_manager.InstallError):
        engine_manager._expected_hash(sums, "other.tar.xz")


def test_asset_names_and_platform():
    assert engine_manager.asset_names("5.11.0", "aarch64") == (
        "curaengine-5.11.0-linux-aarch64", "curaengine-5.11.0-linux-aarch64.tar.xz")
    arch, problem = engine_manager.check_platform()
    assert arch in ("aarch64", "x86_64", None)


def test_https_required(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_manager, "check_platform", lambda: ("x86_64", None))
    with pytest.raises(engine_manager.InstallError):
        engine_manager.download_and_install("http://example.invalid/x", str(tmp_path))
