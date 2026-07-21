"""Piggyback test: an ORDINARY geometry-nodes scatter, only tagged.

No point cloud, no pscale/orient attributes, no authoring convention -- the
exact same GN Instance-on-Points object used as the baseline, with one custom
property ticked. The patched Cycles should read the unrealized instances that
GN already built (the packed-primitive form) instead of letting
object_duplilist expand them.

A/B is the same object with the tag on vs off, alternating in one process, so
load drift cancels.

Usage:
    blender -b --debug-cycles -P bench_gn_piggyback.py -- <n_side> [renders]
"""

import bpy
import sys
import time

sys.path.insert(0, __file__.rsplit("\\", 1)[0])
from build_instancer_scene import (  # noqa: E402
    clear_scene,
    make_prototype,
    make_gn_baseline,
)

MARKER = "cycles_render_instancer"


def setup(n_side):
    clear_scene()
    proto = make_prototype()
    gn = make_gn_baseline(proto, n_side)

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
    scene.render.use_persistent_data = True
    return gn, cam


def run(gn, cam, tagged, n_renders):
    if tagged:
        gn[MARKER] = True
    elif MARKER in gn:
        del gn[MARKER]
    gn.update_tag()
    bpy.context.view_layer.update()

    times = []
    for i in range(n_renders):
        if i > 0:
            cam.location.x += 0.01
            bpy.context.view_layer.update()
        t = time.perf_counter()
        bpy.ops.render.render(write_still=False)
        times.append((time.perf_counter() - t) * 1000.0)
    return times


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    n_side = int(argv[0]) if argv else 700
    n_renders = int(argv[1]) if len(argv) > 1 else 3

    gn, cam = setup(n_side)

    print(f"### PLAIN GN SCATTER, {n_side * n_side:,} instances")
    print("### --- tag OFF (normal dupli path) ---")
    off = run(gn, cam, False, n_renders)
    print(f"### off re-syncs: {[round(t) for t in off[1:]]}")

    print("### --- tag ON (piggyback on GN instances) ---")
    on = run(gn, cam, True, n_renders)
    print(f"### on  re-syncs: {[round(t) for t in on[1:]]}")

    b, y = min(off[1:]), min(on[1:])
    print("### " + "=" * 56)
    print(f"### dupli path   : {b:9.1f} ms")
    print(f"### piggyback    : {y:9.1f} ms")
    print(f"### SPEEDUP      : {b / y:9.2f}x" if y else "")
    print("### " + "=" * 56)


if __name__ == "__main__":
    main()
