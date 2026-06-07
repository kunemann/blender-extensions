# koen.work — Blender Extensions Repository

A **self-hosted Blender extensions repository**. It's just static files (the `.zip`
extensions + a generated `index.json`) served over HTTPS. No server, no subscription,
no running process — any static host works (GitHub Pages, Cloudflare Pages, your own
website). Blender 4.2+ reads the `index.json` and lets users install & auto-update
every add-on with one click.

## What's inside `docs/`

| File | Extension | Min. Blender |
|------|-----------|--------------|
| `multi_range_renderer-1.2.0.zip` | Multi Range Renderer | 4.2 |
| `exreplace-1.0.0.zip` | EXReplace | 4.5 |
| `layercake-1.0.0.zip` | LayerCake | 4.2 |
| `patchwork-0.2.1.zip` | Patchwork | 5.0 |
| `index.json` | repository index (generated — do not edit by hand) | |
| `index.html` | human-readable listing (generated) | |

Only the `docs/` folder gets published (GitHub Pages serves it). Everything else
(`build.sh`, `.build/`, this README) is for maintenance and stays unpublished.

## Hosting (GitHub Pages)

This repo is published with **GitHub Pages → Branch `main`, folder `/docs`** (free).
The public repository URL users enter in Blender is:

```
https://<your-github-user>.github.io/<repo-name>/index.json
```

A custom domain (e.g. `https://extensions.koen.work/index.json`) can be added later in
the repo's **Settings → Pages → Custom domain**.

## How users install your add-ons

1. In Blender: **Edit → Preferences → Get Extensions**.
2. Top-right dropdown (⌄) → **Repositories** → **+** → **Add Remote Repository**.
3. Paste the repository URL (ends in `/index.json`), e.g.
   `https://<your-host>/index.json`. Enable **Check for Updates on Startup**.
4. Back in **Get Extensions**, your add-ons now appear — click **Install**.

They can also **drag a single `.zip` straight into Blender** to install it (this is what
the "Extension" button on the koen.work add-ons page does — it points at one of these zips).

## Adding or updating an extension

1. Drop the new/updated `.zip` into `docs/`. (For a new version, just add the new zip —
   keep or remove the old one; Blender always offers the newest.)
2. Run `./build.sh` — it rebuilds `index.json` (hashes, sizes, metadata) with Blender.
3. Commit & push (or re-upload `repo/`). Users get the update automatically.

Each extension `.zip` must contain a `blender_manifest.toml` **at its root**. A plain
legacy add-on (a `.py` with `bl_info`) must be converted first — see `.build/` for how
Multi Range Renderer was wrapped (rename the script to `__init__.py`, add a manifest,
then `blender --command extension build --source-dir <dir> --output-dir docs`).

## Not included

- **CarbonicAcid_…blend** — a `.blend` asset, not an add-on. Asset files can't live in an
  extensions repo; distribute it as a normal download instead.
- **UV Packmaster** — a commercial third-party product (and it ships separate engine
  binaries). Don't redistribute it here unless its license explicitly allows it.
- **RenderBones** — distributed separately on Superhive Market, not redistributed here.

## Regenerating from scratch

```bash
./build.sh        # auto-detects Blender, or: BLENDER=/path/to/blender ./build.sh
```
