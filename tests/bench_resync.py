"""The measurement that actually matches the brief: RE-sync cost.

The first Cycles sync allocates N Object nodes (create_node<Object>()), and at
1M that allocation dominates everything else -- but the GN baseline pays it
too, and it is NOT what makes the viewport stall. The brief's complaint is
about the SECOND and subsequent syncs: touch something unrelated (select the
camera), and Cycles re-walks the instance list before it can redraw.

On re-sync, id_map::add_or_update finds the existing Object and skips
allocation, so what remains is:
    baseline : object_duplilist expansion  +  N map lookups
    bypass   : N map lookups               (no duplilist)

render.use_persistent_data keeps the Cycles session (and its object_map) alive
between renders, which is what lets us observe a genuine re-sync headlessly.

Usage:
    blender -b --debug-cycles -P bench_resync.py -- <n_side> [renders]
"""

import bpy
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from build_instancer_scene import (  # noqa: E402
    clear_scene,
    make_prototype,
    make_gn_baseline,
    make_render_instancer,
)


def setup(n_side, use_bypass):
    clear_scene()
    proto = make_prototype()
    gn = make_gn_baseline(proto, n_side)
    ri = make_render_instancer(proto, n_side)

    cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
    bpy.context.collection.objects.link(cam)
    cam.location = (0.0, -120.0, 60.0)
    cam.rotation_euler = (1.1, 0.0, 0.0)

    scene = bpy.context.scene
    scene.camera = cam
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    scene.render.resolution_x = 32
    scene.render.resolution_y = 32
    scene.cycles.use_adaptive_sampling = False
    # The whole point: keep the Cycles session alive so the 2nd render is a
    # genuine incremental re-sync rather than a fresh build.
    scene.render.use_persistent_data = True

    target = ri if use_bypass else gn
    other = gn if use_bypass else ri
    target.hide_render = False
    other.hide_render = True
    return cam


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    n_side = int(argv[0]) if argv else 1000
    n_renders = int(argv[1]) if len(argv) > 1 else 4
    use_bypass = "--bypass" in argv
    label = "BYPASS (point cloud)" if use_bypass else "BASELINE (GN dupli)"

    cam = setup(n_side, use_bypass)

    times = []
    for i in range(n_renders):
        if i > 0:
            # Touch something unrelated to the scatter, exactly like selecting
            # the camera in the viewport. This is the stall trigger.
            cam.location.x += 0.01
            bpy.context.view_layer.update()
        t = time.perf_counter()
        bpy.ops.render.render(write_still=False)
        times.append((time.perf_counter() - t) * 1000.0)

    print("")
    print("=" * 70)
    print(f"  {label}   {n_side * n_side:,} instances")
    print("-" * 70)
    print(f"  render 1 (cold sync)  : {times[0]:9.1f} ms")
    for i, t in enumerate(times[1:], start=2):
        print(f"  render {i} (re-sync)    : {t:9.1f} ms")
    resyncs = times[1:]
    if resyncs:
        best = min(resyncs)
        print("-" * 70)
        print(f"  best re-sync          : {best:9.1f} ms   <-- THE STALL METRIC")
        print(f"CSV_RESYNC,{'bypass' if use_bypass else 'baseline'},"
              f"{n_side * n_side},{times[0]:.1f},{best:.1f}")
    print("=" * 70)


if __name__ == "__main__":
    main()
