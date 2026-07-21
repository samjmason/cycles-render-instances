import bpy, bmesh, sys
sys.path.insert(0, __file__.rsplit("\\",1)[0])
from build_instancer_scene import clear_scene

def prim(name, kind, loc):
    me = bpy.data.meshes.new(name+"M"); ob = bpy.data.objects.new(name, me)
    bm = bmesh.new()
    if kind == 0: bmesh.ops.create_cube(bm, size=0.6)
    elif kind == 1: bmesh.ops.create_icosphere(bm, subdivisions=1, radius=0.4)
    else: bmesh.ops.create_cone(bm, segments=8, radius1=0.4, radius2=0.0, depth=0.8, cap_ends=True)
    bm.to_mesh(me); bm.free()
    ob.location = loc
    return ob

def build(n_side, tagged):
    clear_scene()
    coll = bpy.data.collections.new("ProtoCollection")
    bpy.context.scene.collection.children.link(coll)
    # three members at distinct offsets so a dropped member is visible
    for i, off in enumerate([(0,0,0), (1.2,0,0), (0,1.2,0)]):
        ob = prim(f"Member{i}", i, off)
        coll.objects.link(ob)
    coll.instance_offset = (0.0, 0.0, 0.0)

    scatter = bpy.data.objects.new("CollScatter", bpy.data.meshes.new("SM"))
    bpy.context.scene.collection.objects.link(scatter)
    mod = scatter.modifiers.new("GN","NODES")
    tree = bpy.data.node_groups.new("T","GeometryNodeTree"); mod.node_group = tree
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    out = tree.nodes.new("NodeGroupOutput")
    grid = tree.nodes.new("GeometryNodeMeshGrid")
    grid.inputs["Size X"].default_value=60.0; grid.inputs["Size Y"].default_value=60.0
    grid.inputs["Vertices X"].default_value=n_side; grid.inputs["Vertices Y"].default_value=n_side
    iop = tree.nodes.new("GeometryNodeInstanceOnPoints")
    ci = tree.nodes.new("GeometryNodeCollectionInfo")
    ci.inputs["Collection"].default_value = coll
    ci.inputs["Separate Children"].default_value = False
    tree.links.new(grid.outputs["Mesh"], iop.inputs["Points"])
    tree.links.new(ci.outputs["Instances"], iop.inputs["Instance"])
    tree.links.new(iop.outputs["Instances"], out.inputs["Geometry"])
    if tagged: scatter["cycles_render_instancer"] = True

    sun = bpy.data.objects.new("Sun", bpy.data.lights.new("S","SUN"))
    bpy.context.scene.collection.objects.link(sun); sun.rotation_euler=(0.6,0.2,0); sun.data.energy=4
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("C"))
    bpy.context.scene.collection.objects.link(cam); cam.location=(0,-90,55); cam.rotation_euler=(1.05,0,0)
    s = bpy.context.scene; s.camera=cam; s.render.engine='CYCLES'; s.cycles.device='CPU'
    s.cycles.samples=16; s.render.resolution_x=240; s.render.resolution_y=160
    s.cycles.use_adaptive_sampling=False
    return scatter, s
