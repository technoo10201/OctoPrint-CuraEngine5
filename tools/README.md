# Development tools

## `uranium_reference.py` — reference values computed by Uranium

The plugin's resolver (`octoprint_curaengine5/resolver`) is a pure-Python
reimplementation of Cura's setting resolution. To check that it produces exactly the
same values, this script runs **the real Uranium and Cura code** (LGPL-3.0, read from
an extracted UltiMaker Cura AppImage, never copied into this repository) on the
scenarios in `tests/reference/*.scenario.json`, and writes the `*.reference.json`
files that `tests/test_resolver_reference.py` compares against.

Only `CuraApplication`, `UM.Application` and a few services (container registry,
version upgrades) are replaced by small stubs; the stacks (`GlobalStack`,
`ExtruderStack`), `SettingFunction`, `CuraFormulaFunctions` and `XmlMaterialProfile`
are those of Cura 5.11.

### Preparing the environment

```bash
./UltiMaker-Cura-5.11.0-linux-X64.AppImage --appimage-extract   # produces squashfs-root/
uv venv --python 3.12 refvenv          # Python 3.12, like Cura 5.11
VIRTUAL_ENV=refvenv uv pip install "PyQt6==6.8.*" cryptography "numpy<2"
```

### Regenerating the references

```bash
tools/make_references.sh refvenv/bin/python squashfs-root
```

### Scenario format

```json
{
  "machine": "eryone_thinker",
  "global": {
    "user": {"layer_height": "0.16"},
    "quality_changes": "../data/abs_global_qc.inst.cfg",
    "quality": "res:quality/eryone_thinker/eryone_thinker_normal.inst.cfg",
    "definition_changes": "../data/eryone_thinker_definition_changes.inst.cfg"
  },
  "extruders": [
    {
      "definition": "eryone_thinker_extruder_0",
      "containers": {
        "material": {"xml": "res:materials/generic_abs_175.xml.fdm_material"}
      }
    }
  ]
}
```

Each layer is either a dictionary of values (text, or a formula starting with `=`),
an `.inst.cfg` file (path relative to the scenario, or `res:` for Cura resources), or
an XML material (`{"xml": ...}`).
