# Blender Extensions

Extensions for Blender 4.2 and newer, kept compatible with the latest releases (5.0, 5.1, 5.2 LTS) and the 5.3 beta.

## Add-ons

| Add-on | Description | Version | Min. Blender | Checked up to |
|--------|-------------|:-------:|:------------:|:-------------:|
| Multi Range Renderer | Render multiple frame ranges with selected cameras | 1.2.1 | 4.2 | 5.3 beta |
| LayerCake | Rebuild the Combined image from render passes | 1.1.1 | 4.2 | 5.3 beta |
| Patchwork | Multi-pattern anti-tiling for image textures (Voronoi / Noise / Gabor / Blocks / Rings) | 0.2.2 | 5.0 | 5.3 beta |
| EXReplace | Swap Render Layer and multilayer EXR connections in the Compositor | 1.0.1 | 4.5 | 5.3 beta |
| CopyRender | Copy, paste and sync render settings via clipboard or file | 1.3.1 | 4.2 | 5.3 beta |
| Listen, Children! | Sync visibility between parent and child objects | 1.5.1 | 4.2 | 5.3 beta |
| LockNodes | Password-lock node groups for preset pack distribution | 1.1.1 | 4.5 | 5.3 beta |

## Compatibility

Every add-on is checked against the Python API changes of each Blender release (5.0 unified compositor / removal of legacy APIs, 5.1 Python 3.13, 5.2 LTS, 5.3 beta) and smoke-tested on 5.2 LTS. Pure-Python only, so no per-platform builds are needed.

## Releases

Each add-on has its own [GitHub release](https://github.com/kunemann/blender-extensions/releases) (tag `<id>-v<version>`) with the installable `.zip` attached.

## Install in Blender

Add the repository so the add-ons install and update from inside Blender:

1. **Edit → Preferences → Get Extensions**.
2. Dropdown (⌄) → **Repositories** → **＋** → **Add Remote Repository**.
3. Paste this URL:

   ```
   https://raw.githubusercontent.com/kunemann/blender-extensions/main/docs/index.json
   ```

4. Open **Get Extensions** and install any add-on from the list above.

Or drag an add-on's `.zip` into a running Blender 4.2+ window to install it directly.
