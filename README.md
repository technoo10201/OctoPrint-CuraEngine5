# OctoPrint-CuraEngine5

An [OctoPrint](https://octoprint.org) plugin that slices STL files with **CuraEngine 5.x** (tested with 5.11.0), directly from OctoPrint. It succeeds "CuraEngine Legacy", which only supports CuraEngine 15.04.

> **Unofficial project.** "Cura" and "UltiMaker" are trademarks of UltiMaker. This project is not affiliated with or endorsed by UltiMaker. CuraEngine is free software by UltiMaker, licensed under AGPL-3.0.

## Features

- **Slices with CuraEngine 5** from the command line, with progress reporting and cancellation in OctoPrint.
- **Resolves every setting the way Cura does.** On the command line, CuraEngine does not evaluate the `value`/`resolve` formulas of the definition files. The plugin ships a pure-Python resolver (no Uranium, no Qt, so it runs on a Raspberry Pi) that reproduces Cura's container stacks (machine definition < machine changes < variant < material < quality < intent < profile < user settings), per-extruder stacks, `resolve`, `limit_to_extruder` and the formula functions (`extruderValue`, `extruderValues`, `resolveOrValue`, ...). Its output is checked automatically against the real Uranium/Cura 5.11 code: **zero differences** on the test scenarios (about 700 settings each).
- **Mirrors Cura's slice preparation**: `{setting}`, `{setting, extruder}` and `{if ...}...{endif}` tokens in start/end G-code, no extra preheat when your start G-code already sets temperatures, the final header (`;TIME`, `;Filament used`, ...) and post-slice variables (`{print_time}`, `{filament_amount}`, `{jobname}`, ...).
- **Imports your Cura profiles**: `.curaprofile`, `.inst.cfg`, or any G-code written by Cura (settings are read back from the trailing `;SETTING_3` block), including a G-code file already stored in OctoPrint.
- **Uses Cura's resources**: 600+ printer definitions, qualities, intents, nozzle variants and XML materials (`.xml.fdm_material`).
- **Slicer tab** in OctoPrint's interface to pick a printer and profile, load a model, place it on the bed in a 3D view and slice.

## Requirements

- OctoPrint 1.10 or later (tested with 1.11.8), Python 3.7+.
- A **CuraEngine 5.x** binary for the machine running OctoPrint, plus the matching **`share/cura/resources`** folder from the same Cura version.
- 64-bit Linux with **glibc 2.35 or later** for the CuraEngine shipped in UltiMaker's 5.11 AppImage (glibc 2.36 for the binaries built by this repository's workflow). OctoPi 1.1 / Raspberry Pi OS Bookworm (glibc 2.36) works; the official `octoprint/octoprint` Docker image (Debian 11, glibc 2.31) does **not**.

### Getting CuraEngine

- **x86_64 PC**: extract the official AppImage (`./UltiMaker-Cura-5.11.0-linux-X64.AppImage --appimage-extract`) and point the plugin to `squashfs-root/CuraEngine` as the engine and `squashfs-root/share/cura/resources` as the resources. The plugin detects that this binary needs the dynamic loader and its bundled libraries, and starts it accordingly.
- **Raspberry Pi (aarch64)**: UltiMaker does not publish ARM builds. The [`build-curaengine.yml`](.github/workflows/build-curaengine.yml) workflow builds CuraEngine for aarch64 and x86_64 and publishes an "engine + resources" archive. The "Download and install CuraEngine" button on the settings page installs it into the plugin's data folder after a SHA-256 check. Nothing is downloaded unless you click it.
- A CuraEngine you built yourself works too.

## Installation

Once the repository is published, use *Plugin Manager > Get More... > from URL* with the address of the repository archive. For development:

```bash
pip install -e .
```

## Configuration

In *Settings > CuraEngine 5*:

1. **Engine and resources**: paths to the CuraEngine binary and to Cura's resources folder (auto-detected when possible).
2. **Machine**: the Cura definition of your printer (for example "Eryone Thinker Series"), the build volume and nozzle if you want to override the definition's, the material, and your start and end G-code. With "Custom FFF printer", the dimensions of the OctoPrint printer profile are used.
3. **Import a Cura profile**: choose a `.curaprofile` or a Cura G-code file, give it a name, and tick the default-profile option if needed.

## Usage

Open the **Slicer** tab:

1. Choose the printer and the profile (the default profile is marked with a star).
2. Pick a model among your OctoPrint files and load it into the 3D view.
3. Adjust its placement (position, rotation, scale) on the build plate.
4. Choose the output file name and the support option, then start slicing. Progress is shown in OctoPrint and the job can be cancelled.

You can also slice from OctoPrint's file list: the "Slice" icon of an STL offers the "CuraEngine 5" slicer and your profiles.

## Current limitations (first milestone)

- Only one extruder is driven by the interface (the resolver handles several).
- One model per slice, without per-object settings.
- Profiles created with a Cura version whose setting version differs from 25 (Cura 5.11) are not converted: settings renamed in the meantime are ignored, and a warning is shown.
- Nozzle-specific settings of XML materials are applied only when a nozzle variant is selected.

## Screenshots

Screenshots of the Slicer tab and of the settings page will be added to this repository.

## Development and tests

```bash
uv venv --python 3.11 .venv && uv pip install -e . octoprint pytest
.venv/bin/python -m pytest -q
```

Tests that need the engine or Cura's resources look for them through `CURAENGINE5_CURA_APPDIR` (extracted AppImage), or `CURAENGINE5_ENGINE` and `CURAENGINE5_RESOURCES`, and are skipped otherwise. The [`tools/`](tools/README.md) folder contains the script that computes reference values with the real Uranium code.

## License

[AGPL-3.0-or-later](LICENSE). Cura's resources (definitions, qualities) are under LGPL-3.0 and the `fdm_materials` files under their own license; they are not included in this repository, except as test data.

## Non-affiliation

This is an independent project by the GitHub user `technoo10201`. It is not affiliated with, sponsored by or endorsed by UltiMaker. "Cura" and "UltiMaker" are trademarks of UltiMaker.
