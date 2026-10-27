import bpy

from . import pass_mapping


_RLAYERS = "CompositorNodeRLayers"
_IMAGE = "CompositorNodeImage"


def _switch_composite_to_view_layer(image_node, context):
    """If the multilayer EXR Image node has Layer set to 'Composite', switch
    it to 'View Layer' so the per-pass output sockets become available."""
    try:
        if image_node.layer != "Composite":
            return False
        image_node.layer = "View Layer"
    except (AttributeError, TypeError, ValueError):
        return False
    if image_node.image is not None:
        try:
            image_node.image.reload()
        except RuntimeError:
            pass
    context.view_layer.update()
    print(f"[EXReplace] Switched '{image_node.name}' layer: Composite -> View Layer")
    return True


class EXREPLACE_OT_swap_connections(bpy.types.Operator):
    bl_idname = "exreplace.swap_connections"
    bl_label = "EXReplace: Swap Render Layer / EXR Connections"
    bl_description = (
        "Move all outgoing links between a Render Layer node and a multilayer "
        "EXR Image node. The node with outgoing links is the source; "
        "the other becomes the new source"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        sd = context.space_data
        return (
            sd is not None
            and sd.type == "NODE_EDITOR"
            and sd.tree_type == "CompositorNodeTree"
            and sd.edit_tree is not None
        )

    def execute(self, context):
        tree = context.space_data.edit_tree
        selected = [n for n in tree.nodes if n.select]

        if len(selected) != 2:
            self.report(
                {"ERROR"},
                "Select exactly two nodes (Render Layer + EXR Image)",
            )
            return {"CANCELLED"}

        types = {n.bl_idname for n in selected}
        if types != {_RLAYERS, _IMAGE}:
            self.report(
                {"ERROR"},
                "Selection must contain exactly one Render Layer and one Image node",
            )
            return {"CANCELLED"}

        # If any selected Image node sits on the "Composite" layer, switch it
        # to "View Layer" before we look at the tree. The Composite layer only
        # exposes Image/Alpha; switching reveals the per-pass outputs.
        for n in selected:
            if n.bl_idname == _IMAGE:
                _switch_composite_to_view_layer(n, context)

        # Compare nodes by name (unique within tree). bpy returns fresh proxy
        # wrappers on each attribute access, so identity/equality between
        # different reads is not reliable.
        a, b = selected
        a_name, b_name = a.name, b.name

        all_links = list(tree.links)
        a_outgoing = [l for l in all_links if l.from_node.name == a_name]
        b_outgoing = [l for l in all_links if l.from_node.name == b_name]

        if a_outgoing and not b_outgoing:
            source, target, source_links = a, b, a_outgoing
        elif b_outgoing and not a_outgoing:
            source, target, source_links = b, a, b_outgoing
        elif a_outgoing and b_outgoing:
            active = tree.nodes.active
            if active is None or active.name not in (a_name, b_name):
                self.report(
                    {"ERROR"},
                    "Both nodes have outgoing links - make the target the active node (click it last)",
                )
                return {"CANCELLED"}
            if active.name == a_name:
                source, target, source_links = b, a, b_outgoing
            else:
                source, target, source_links = a, b, a_outgoing
        else:
            self.report(
                {"WARNING"},
                f"Neither selected node has outgoing links (tree '{tree.name}', "
                f"{len(all_links)} links total)",
            )
            return {"CANCELLED"}

        if target.bl_idname == _IMAGE:
            if target.image is None:
                self.report({"ERROR"}, "EXR Image node has no image assigned")
                return {"CANCELLED"}
            if len(target.outputs) <= 2:
                try:
                    target.image.reload()
                except RuntimeError:
                    pass
                context.view_layer.update()

        # Snapshot link properties as plain strings BEFORE any mutation.
        # tree.links.new()/remove() can invalidate other link wrappers in
        # `source_links`, which made reverse swaps flaky. Stable strings let
        # us re-resolve sockets fresh every iteration.
        source_name = source.name
        target_name = target.name
        work = [
            {
                "from_id": l.from_socket.identifier,
                "from_name": l.from_socket.name,
                "from_type": l.from_socket.bl_idname,
                "to_node": l.to_node.name,
                "to_id": l.to_socket.identifier,
                "to_name": l.to_socket.name,
            }
            for l in source_links
        ]

        transferred = []
        skipped = []

        print(f"\n[EXReplace] {source_name} -> {target_name}")
        print(f"  target outputs:")
        for s in target.outputs:
            print(f"    name='{s.name}'  id='{s.identifier}'  type={s.bl_idname}  enabled={s.enabled}")

        for item in work:
            from_name = item["from_name"]
            from_id = item["from_id"]
            from_type = item["from_type"]

            src_node = tree.nodes.get(source_name)
            if src_node is None:
                skipped.append(f"{from_name}: source node gone")
                continue
            src_sock = next(
                (s for s in src_node.outputs if s.identifier == from_id),
                None,
            )
            if src_sock is None:
                skipped.append(f"{from_name}: source socket gone")
                continue

            match = pass_mapping.find_match(src_sock, list(target.outputs))
            if match is None:
                reason = f"{from_name}: no match (id='{from_id}')"
                skipped.append(reason)
                print(f"  SKIP {reason}")
                continue

            to_node = tree.nodes.get(item["to_node"])
            if to_node is None:
                reason = f"{from_name}: downstream node '{item['to_node']}' gone"
                skipped.append(reason)
                print(f"  SKIP {reason}")
                continue
            to_sock = next(
                (s for s in to_node.inputs if s.identifier == item["to_id"]),
                None,
            )
            if to_sock is None:
                reason = f"{from_name}: downstream input gone"
                skipped.append(reason)
                print(f"  SKIP {reason}")
                continue

            try:
                new_link = tree.links.new(match, to_sock)
            except (RuntimeError, ReferenceError) as exc:
                reason = f"{from_name}: {exc}"
                skipped.append(reason)
                print(f"  ERROR {reason}")
                continue

            if new_link is None:
                reason = f"{from_name}: link rejected ({from_type} -> {match.bl_idname})"
                skipped.append(reason)
                print(f"  REJECT {reason}")
                continue

            # Remove leftover original link if Blender did not auto-replace it.
            for l in list(tree.links):
                if (
                    l.from_node.name == source_name
                    and l.from_socket.identifier == from_id
                    and l.to_node.name == item["to_node"]
                    and l.to_socket.identifier == item["to_id"]
                ):
                    try:
                        tree.links.remove(l)
                    except (RuntimeError, ReferenceError):
                        pass
                    break

            transferred.append(from_name)
            arrow = " == " if from_name == match.name else " -> "
            print(f"  OK  {from_name}{arrow}{match.name}  =>  {item['to_node']}.{item['to_name']}")

        if not transferred:
            msg = "No links transferred"
            if skipped:
                msg += f"; skipped: {' | '.join(skipped)}"
            self.report({"WARNING"}, msg)
            return {"CANCELLED"}

        if skipped:
            self.report(
                {"WARNING"},
                f"{len(transferred)} transferred, {len(skipped)} skipped: {' | '.join(skipped)}",
            )
        else:
            self.report({"INFO"}, f"{len(transferred)} links transferred")

        for area in context.screen.areas:
            if area.type == "NODE_EDITOR":
                area.tag_redraw()

        return {"FINISHED"}
