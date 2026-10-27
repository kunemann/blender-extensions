## LayerCake 1.1.1

- Reviewed for Blender 5.0 – 5.2 (uses `scene.compositing_node_group` on 5.x, `scene.node_tree` on 4.x).

Smoke-tested on Blender 5.2.3 LTS (Python 3.13): register/unregister and core functions. Code reviewed against the Python API changes of 5.0, 5.1, 5.2 and the 5.3 beta (no breaking Python API changes for these add-ons).

Install: drag the attached `.zip` into Blender, or add the repository `https://raw.githubusercontent.com/kunemann/blender-extensions/main/docs/index.json` under Preferences → Get Extensions → Repositories.
