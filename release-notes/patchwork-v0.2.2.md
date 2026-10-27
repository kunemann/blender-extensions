## Patchwork 0.2.2

- Compatibility: no longer reads `Material.use_nodes` (deprecated in Blender 5.0, removal planned for 6.0); no deprecation warnings on 5.x.
- Shorter manifest tagline (Blender limit: 64 characters).

Smoke-tested on Blender 5.2.3 LTS (Python 3.13): register/unregister and core functions. Code reviewed against the Python API changes of 5.0, 5.1, 5.2 and the 5.3 beta (no breaking Python API changes for these add-ons).

Install: drag the attached `.zip` into Blender, or add the repository `https://raw.githubusercontent.com/kunemann/blender-extensions/main/docs/index.json` under Preferences → Get Extensions → Repositories.
