"""Baseline harness: cost of Blender's dupli-list expansion for a GN scatter.

Runs headless (blender -b -P this.py -- N) so no GL/draw path is involved --
this isolates the blenkernel object_duplilist / depsgraph instance-iterator cost,
which is the thing the Cycles render-instances patch aims to bypass.

Reports, for instance count N:
  cold      -- first build of the dupli list
  warm      -- re-access with nothing invalidated (should be ~free)
  camera    -- re-access after selecting an UNRELATED object (the camera).
               This is the "stall proxy": if selecting the camera forces a full
               N-instance rebuild, this is ~= cold, and that is the bug.
"""

import bpy
import sys
import time
import ctypes
from ctypes import wintypes


# ---------------------------------------------------------------- win32 memory

class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


# Without explicit restype/argtypes ctypes assumes c_int, which truncates the
# 64-bit process handle and makes GetProcessMemoryInfo silently report zeros.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.GetCurrentProcess.argtypes = []
_psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
_psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
    wintypes.DWORD,
]


def rss_gb():
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    ok = _psapi.GetProcessMemoryInfo(
        _kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        counters.cb,
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.WorkingSetSize / (1024 ** 3)


def avail_gb():
    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullAvailPhys / (1024 ** 3)


# ------------------------------------------------------------------ scene prep

def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def build_scatter(n_side):
    """One GN object instancing a single cube prototype onto an n_side^2 grid."""
    proto = bpy.data.objects.new("Proto", bpy.data.meshes.new("ProtoMesh"))
    bpy.context.collection.objects.link(proto)
    # Give the prototype real geometry; a cube is cheap and BVH-instanced anyway.
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=0.1)
    bm.to_mesh(proto.data)
    bm.free()
    # Keep the prototype itself out of the way of the scatter.
    proto.location = (0.0, 0.0, -100.0)

    scatter = bpy.data.objects.new("Scatter", bpy.data.meshes.new("ScatterMesh"))
    bpy.context.collection.objects.link(scatter)

    mod = scatter.modifiers.new("GN", "NODES")
    tree = bpy.data.node_groups.new("Scatter", "GeometryNodeTree")
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

    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    bpy.context.collection.objects.link(cam)
    return scatter, cam


# ------------------------------------------------------------------- measuring

def build_duplis():
    """Force the dupli expansion in C++ without paying Python per-instance cost.

    Taking only the first element still builds the whole list C++-side, so this
    times the blenkernel work rather than the interpreter's iteration overhead
    (a full Python for-loop over N is far slower than the build itself and would
    completely mask what we're measuring).
    """
    deg = bpy.context.evaluated_depsgraph_get()
    return next(iter(deg.object_instances), None)


def count_instances():
    """Full count -- only for reporting, never inside a timed section."""
    deg = bpy.context.evaluated_depsgraph_get()
    return sum(1 for _ in deg.object_instances)


def timed(label, fn):
    start = time.perf_counter()
    result = fn()
    elapsed = (time.perf_counter() - start) * 1000.0
    return label, elapsed, result


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    n_side = int(argv[0]) if argv else 1000
    target = n_side * n_side

    clear_scene()
    mem_before = rss_gb()
    scatter, cam = build_scatter(n_side)

    # Cook the node tree + build the dupli list for the first time.
    _, t_cold, _ = timed("cold", build_duplis)

    # Nothing invalidated -- this is the "already built, just read it" case.
    _, t_warm, _ = timed("warm", build_duplis)

    # Select an object entirely unrelated to the scatter, then re-read.
    # If this costs ~= cold, any depsgraph touch rebuilds all N instances.
    cam.select_set(True)
    bpy.context.view_layer.objects.active = cam
    bpy.context.view_layer.update()
    _, t_camera, _ = timed("camera", build_duplis)

    mem_after = rss_gb()
    count = count_instances()
    per_m = lambda ms: ms / (target / 1_000_000.0)

    print("")
    print("=" * 66)
    print(f"  N requested : {target:,}   (grid {n_side} x {n_side})")
    print(f"  N instances : {count:,}")
    print("-" * 66)
    print(f"  cold build          : {t_cold:9.1f} ms   ({per_m(t_cold):6.1f} ms/M)")
    print(f"  warm re-access      : {t_warm:9.1f} ms   ({per_m(t_warm):6.1f} ms/M)")
    print(f"  after camera select : {t_camera:9.1f} ms   ({per_m(t_camera):6.1f} ms/M)  <-- STALL PROXY")
    print("-" * 66)
    print(f"  blender RSS         : {mem_before:6.2f} -> {mem_after:6.2f} GB")
    print(f"  system avail RAM    : {avail_gb():6.2f} GB")
    print("=" * 66)
    print(f"CSV,{target},{count},{t_cold:.1f},{t_warm:.1f},{t_camera:.1f},{mem_after:.3f}")


if __name__ == "__main__":
    main()
