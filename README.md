# Blender Extensions Repository

A free, self-hosted **Blender extensions repository** for Blender **4.2 and newer**.

It is just a set of static files — the add-on `.zip` files plus a generated `index.json` —
served over HTTPS via GitHub Pages. Blender reads `index.json` and can install and
**auto-update** every add-on listed here with one click. No account, no server, no
subscription.

## Add this repository to Blender

1. In Blender open **Edit → Preferences → Get Extensions**.
2. Top-right dropdown (⌄) → **Repositories** → **＋** → **Add Remote Repository**.
3. Paste this URL and enable **Check for Updates on Startup**:

   ```
   https://kunemann.github.io/blender-extensions/index.json
   ```
4. Back in **Get Extensions** the add-ons below now appear — click **Install** on any of them.
   Blender will keep them up to date automatically.

### Or install a single add-on by drag-and-drop

Drag an add-on's download link straight into a running Blender 4.2+ window. Blender
installs and enables the add-on right away. Because the links published for this
repository also carry the repository address, Blender additionally offers to add this
repository on first drop — so even a one-off drag-and-drop install leaves Blender with
everything it needs to keep the add-on (and the others here) updated.

## Add-ons in this repository

| Add-on | Description | Min. Blender | License |
|--------|-------------|:------------:|---------|
| **Multi Range Renderer** | Render multiple frame ranges with selected cameras | 4.2 | MIT |
| **LayerCake** | Rebuild the Combined image from render passes | 4.2 | GPL-3.0-or-later |
| **Patchwork** | Multi-pattern anti-tiling for image textures (Voronoi / Noise / Gabor / Blocks / Rings) | 5.0 | MIT |
| **EXReplace** | Swap Render Layer and multilayer EXR connections in the Compositor | 4.5 | MIT |

Each add-on's own license is declared inside its `blender_manifest.toml`. The repository
as a whole is licensed **GPL-3.0** — see [`LICENSE`](LICENSE).

## Repository layout

```
docs/            # the published site — GitHub Pages serves this folder
  *.zip          # one Blender extension per file
  index.json     # the repository index Blender reads (generated — do not edit by hand)
  index.html     # human-readable listing (generated)
build.sh         # regenerates index.json + index.html from the zips
```

Only `docs/` is published. Everything else (`build.sh`, this README) is for maintenance.

## Maintaining — adding or updating an extension

1. Drop the new or updated `.zip` into `docs/`. Each zip must contain a
   `blender_manifest.toml` at its root.
2. Run `./build.sh` (auto-detects Blender, or `BLENDER=/path/to/blender ./build.sh`). It
   re-reads every zip, recomputes hashes and sizes, and rewrites `index.json` + `index.html`.
3. Commit and push. Anyone who has added the repository in Blender gets the update
   automatically.

A plain legacy add-on (a single `.py` with `bl_info`) must be wrapped into an extension
first: rename the script to `__init__.py`, add a `blender_manifest.toml`, then run
`blender --command extension build --source-dir <dir> --output-dir docs`.

## Custom domain (optional)

A custom domain (e.g. `https://extensions.example.com/index.json`) can be configured under
the repository's **Settings → Pages → Custom domain**.
