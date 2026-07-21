"""Build the M0 test scene: a marked PointCloud render-instance source.

Creates, side by side and with identical instance counts:
  * "Scatter"          -- GN Instance-on-Points (the BASELINE, goes through
                          object_duplilist)
  * "RenderInstancer"  -- a PointCloud carrying the custom-property marker that
                          the patched BlenderSync picks up (the BYPASS path)

Only one is enabled at a time (via hide_render/hide_viewport) so each can be
measured in isolation against the same prototype and the same N.

Usage:
    blender -b -P build_instancer_scene.py -- <n_side> [--save out.blend]

Note: this only AUTHORS data. Stock Blender ignores the custom properties
entirely and will render the point cloud as plain points; only the patched
build acts on them. That is deliberate -- it means the same .blend is a valid
A/B test across both binaries.
"""

import bpy
import sys
import bmesh


MARKER = "cycles_render_instancer"
PROTO_PROP = "cycles_instance_object"


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_prototype():
    """A small cube. Cheap, and BVH-instanced so its complexity is ~free."""
    proto = bpy.data.objects.new("Proto", bpy.data.meshes.new("ProtoMesh"))
    bpy.context.collection.objects.link(proto)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=0.1)
    bm.to_mesh(proto.data)
    bm.free()
    # Park it away from the scatter so it doesn't pollute the render.
    proto.location = (0.0, 0.0, -100.0)
    return proto


def grid_positions(n_side, extent=100.0):
    """Flat [x,y,z, x,y,z, ...] for an n_side x n_side grid on Z=0."""
    coords = []
    step = extent / max(n_side - 1, 1)
    half = extent * 0.5
    for iy in range(n_side):
        y = iy * step - half
        for ix in range(n_side):
            coords.append(ix * step - half)
            coords.append(y)
            coords.append(0.0)
    return coords


def make_gn_baseline(proto, n_side):
    """The control: standard GN Instance-on-Points."""
    ob = bpy.data.objects.new("Scatter", bpy.data.meshes.new("ScatterMesh"))
    bpy.context.collection.objects.link(ob)

    mod = ob.modifiers.new("GN", "NODES")
    tree = bpy.data.node_groups.new("ScatterTree", "GeometryNodeTree")
    mod.node_group = tree

    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    out = tree.nodes.new("NodeGroupOutput")

    grid = tree.nodes.new("GeometryNodeMeshGrid")
    grid.inputs["Size X"].default_value = 100.0
    grid.inputs["Size Y"].default_value = 100.0
    grid.inputs["Vertices X"].default_value = n_side
    grid.inputs["Vertices Y"].default_value = n_side

    iop = tree.nodes.new("GeometryNodeInstanceOnPoints")
    obinfo = tree.nodes.new("GeometryNodeObjectInfo")
    obinfo.inputs["Object"].default_value = proto

    tree.links.new(grid.outputs["Mesh"], iop.inputs["Points"])
    tree.links.new(obinfo.outputs["Geometry"], iop.inputs["Instance"])
    tree.links.new(iop.outputs["Instances"], out.inputs["Geometry"])
    return ob


def make_render_instancer(proto, n_side):
    """The bypass path: a PointCloud + custom-property marker."""
    count = n_side * n_side
    pc = bpy.data.pointclouds.new("RenderInstancerData")

    # PointCloud.points is a read-only collection -- allocation is via resize().
    # A "position" FLOAT_VECTOR attribute on the POINT domain appears
    # automatically, so it never needs creating.
    pc.resize(count)

    coords = grid_positions(n_side)
    pc.attributes["position"].data.foreach_set("vector", coords)

    # Uniform scale attribute the patch reads as "pscale".
    if "pscale" not in pc.attributes:
        pc.attributes.new(name="pscale", type="FLOAT", domain="POINT")
    pc.attributes["pscale"].data.foreach_set("value", [1.0] * count)

    ob = bpy.data.objects.new("RenderInstancer", pc)
    bpy.context.collection.objects.link(ob)

    # The markers the patched BlenderSync looks for. No DNA/RNA change needed.
    ob[MARKER] = True
    ob[PROTO_PROP] = proto
    return ob


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    n_side = int(argv[0]) if argv else 1000
    save_path = None
    if "--save" in argv:
        save_path = argv[argv.index("--save") + 1]

    clear_scene()
    proto = make_prototype()
    gn = make_gn_baseline(proto, n_side)
    ri = make_render_instancer(proto, n_side)

    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    bpy.context.collection.objects.link(cam)
    cam.location = (0.0, -120.0, 60.0)
    cam.rotation_euler = (1.1, 0.0, 0.0)
    bpy.context.scene.camera = cam
    bpy.context.scene.render.engine = "CYCLES"

    # Default state: baseline visible, bypass hidden. Flip per measurement.
    ri.hide_viewport = True
    ri.hide_render = True

    print("")
    print("=" * 62)
    print(f"  grid {n_side} x {n_side} = {n_side * n_side:,} instances")
    print(f"  baseline GN object : {gn.name}  (visible)")
    print(f"  bypass pointcloud  : {ri.name}  (hidden)")
    print(f"  marker             : {MARKER}={ri.get(MARKER)}")
    print(f"  prototype          : {ri.get(PROTO_PROP).name if ri.get(PROTO_PROP) else None}")
    print(f"  pointcloud points  : {len(ri.data.attributes['position'].data):,}")
    print("=" * 62)

    if save_path:
        bpy.ops.wm.save_as_mainfile(filepath=save_path)
        print(f"saved: {save_path}")


if __name__ == "__main__":
    main()
