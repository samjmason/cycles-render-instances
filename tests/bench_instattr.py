"""Instancer Attribute node parity.

GN stores a random colour per instance; the material reads it back through an
Attribute node with type='INSTANCER' and drives base colour with it.

Two assertions, because either alone is worthless:
  1. the attribute genuinely varies the image (vs a scatter with no attribute)
     -- otherwise a build that ignores attributes entirely would "pass"
  2. bypass == dupli

Usage: blender -b -P bench_instattr.py
"""

import bpy
import bmesh
import os
import sys

SP = __file__.rsplit("\\", 1)[0]
sys.path.insert(0, SP)
from build_instancer_scene import clear_scene  # noqa: E402

MARKER = "cycles_render_instancer"
ATTR = "inst_color"


def make_material():
    mat = bpy.data.materials.new("InstMat")
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    attr = nt.nodes.new("ShaderNodeAttribute")
    attr.attribute_type = "INSTANCER"
    attr.attribute_name = ATTR
    nt.links.new(attr.outputs["Color"], bsdf.inputs["Base Color"])
    return mat


def build(n_side, tagged, with_attr):
    clear_scene()

    proto = bpy.data.objects.new("Proto", bpy.data.meshes.new("PM"))
    bpy.context.collection.objects.link(proto)
    bm = bmesh.new(); bmesh.ops.create_cube(bm, size=1.4); bm.to_mesh(proto.data); bm.free()
    proto.location = (0.0, 0.0, -500.0)
    proto.data.materials.append(make_material())

    scatter = bpy.data.objects.new("Scatter", bpy.data.meshes.new("SM"))
    bpy.context.collection.objects.link(scatter)
    mod = scatter.modifiers.new("GN", "NODES")
    tree = bpy.data.node_groups.new("T", "GeometryNodeTree")
    mod.node_group = tree
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    out = tree.nodes.new("NodeGroupOutput")

    grid = tree.nodes.new("GeometryNodeMeshGrid")
    grid.inputs["Size X"].default_value = 40.0
    grid.inputs["Size Y"].default_value = 40.0
    grid.inputs["Vertices X"].default_value = n_side
    grid.inputs["Vertices Y"].default_value = n_side

    iop = tree.nodes.new("GeometryNodeInstanceOnPoints")
    oi = tree.nodes.new("GeometryNodeObjectInfo")
    oi.inputs["Object"].default_value = proto
    tree.links.new(grid.outputs["Mesh"], iop.inputs["Points"])
    tree.links.new(oi.outputs["Geometry"], iop.inputs["Instance"])

    tail = iop.outputs["Instances"]
    if with_attr:
        # Random colour per instance, stored on the INSTANCE domain.
        store = tree.nodes.new("GeometryNodeStoreNamedAttribute")
        store.domain = "INSTANCE"
        store.data_type = "FLOAT_COLOR"
        store.inputs["Name"].default_value = ATTR
        rnd = tree.nodes.new("FunctionNodeRandomValue")
        rnd.data_type = "FLOAT_VECTOR"
        rnd.inputs[0].default_value = (0.0, 0.0, 0.0)
        rnd.inputs[1].default_value = (1.0, 1.0, 1.0)
        idx = tree.nodes.new("GeometryNodeInputIndex")
        tree.links.new(idx.outputs["Index"], rnd.inputs["ID"])
        tree.links.new(tail, store.inputs["Geometry"])
        tree.links.new(rnd.outputs["Value"], store.inputs["Value"])
        tail = store.outputs["Geometry"]
    tree.links.new(tail, out.inputs["Geometry"])

    if tagged:
        scatter[MARKER] = True

    sun = bpy.data.objects.new("Sun", bpy.data.lights.new("S", "SUN"))
    bpy.context.collection.objects.link(sun)
    sun.rotation_euler = (0.5, 0.1, 0.0); sun.data.energy = 5
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("C"))
    bpy.context.collection.objects.link(cam)
    cam.location = (0.0, -55.0, 40.0); cam.rotation_euler = (0.95, 0.0, 0.0)

    s = bpy.context.scene
    s.camera = cam
    s.render.engine = "CYCLES"; s.cycles.device = "CPU"; s.cycles.samples = 24
    s.render.resolution_x = 220; s.render.resolution_y = 150
    s.cycles.use_adaptive_sampling = False
    return scatter, s


def shot(s, tag):
    s.render.filepath = os.path.join(SP, "ia_" + tag)
    bpy.ops.render.render(write_still=True)
    im = bpy.data.images.load(s.render.filepath + ".png")
    px = list(im.pixels); bpy.data.images.remove(im); return px


def report(name, a, b, expect_same=True):
    d = [abs(a[i] - b[i]) for i in range(len(a))]
    mean, mx = sum(d) / len(d), max(d)
    same = mx < 1e-6
    ok = same == expect_same
    print(f"### [{'PASS' if ok else 'FAIL'}] {name}: "
          f"{'identical' if same else f'differ (mean {mean:.5f} max {mx:.5f})'}")
    return ok


res = []
_, s = build(14, False, False); no_attr = shot(s, "noattr")
_, s = build(14, False, True);  dupli = shot(s, "dupli")
res.append(report("instancer attribute varies the image at all", no_attr, dupli, expect_same=False))

_, s = build(14, True, True);   bypass = shot(s, "bypass")
res.append(report("instancer attribute bypass == dupli", dupli, bypass))

print(f"### {sum(res)}/{len(res)} passed")
