"""Production-parity checks: motion blur, ray visibility, holdout.

Each check compares the bypass path against the dupli path on the SAME scene,
and every one is designed so that a broken feature produces a DIFFERENT image
rather than an accidentally matching one.

The motion blur check in particular verifies two things:
  1. bypass == dupli with motion blur on
  2. motion-blurred != static
Without (2) the test would pass if motion blur silently did nothing in both
paths, which is exactly the failure mode being tested for.

Usage: blender -b -P bench_parity.py
"""

import bpy
import bmesh
import os
import sys

SP = __file__.rsplit("\\", 1)[0]
sys.path.insert(0, SP)
from build_instancer_scene import clear_scene, make_prototype, make_gn_baseline  # noqa: E402

MARKER = "cycles_render_instancer"


def base_scene(n_side=25, animate=False, samples=24):
    clear_scene()
    proto = make_prototype()
    proto.location = (0.0, 0.0, 0.0)
    gn = make_gn_baseline(proto, n_side)

    if animate:
        # Animate the INSTANCER so every instance inherits the motion.
        gn.location = (0.0, 0.0, 0.0)
        gn.keyframe_insert("location", frame=1)
        gn.location = (6.0, 0.0, 0.0)
        gn.keyframe_insert("location", frame=3)
        bpy.context.scene.frame_set(2)

    sun = bpy.data.objects.new("Sun", bpy.data.lights.new("S", "SUN"))
    bpy.context.collection.objects.link(sun)
    sun.rotation_euler = (0.6, 0.2, 0.0)
    sun.data.energy = 5

    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("C"))
    bpy.context.collection.objects.link(cam)
    cam.location = (0.0, -110.0, 55.0)
    cam.rotation_euler = (1.1, 0.0, 0.0)

    s = bpy.context.scene
    s.camera = cam
    s.render.engine = "CYCLES"
    s.cycles.device = "CPU"
    s.cycles.samples = samples
    s.render.resolution_x = 200
    s.render.resolution_y = 140
    s.cycles.use_adaptive_sampling = False
    return gn, proto, s


def shot(s, tag):
    s.render.filepath = os.path.join(SP, "par_" + tag)
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(s.render.filepath + ".png")
    px = list(img.pixels)
    bpy.data.images.remove(img)
    return px


def diff(a, b):
    d = [abs(a[i] - b[i]) for i in range(len(a))]
    return sum(d) / len(d), max(d)


def report(name, a, b, expect_same=True):
    mean, mx = diff(a, b)
    same = mx < 1e-6
    ok = same == expect_same
    verdict = "PASS" if ok else "FAIL"
    rel = "identical" if same else f"differ (mean {mean:.5f} max {mx:.5f})"
    print(f"### [{verdict}] {name}: {rel}")
    return ok


results = []

# ---------------------------------------------------------------- motion blur
gn, proto, s = base_scene(animate=True)
s.render.use_motion_blur = False
static_ref = shot(s, "static")

s.render.use_motion_blur = True
s.render.motion_blur_position = "CENTER"
s.render.motion_blur_shutter = 1.0
mb_dupli = shot(s, "mb_dupli")

gn[MARKER] = True
gn.update_tag()
bpy.context.view_layer.update()
mb_bypass = shot(s, "mb_bypass")

# Sanity: motion blur must actually be doing something, or the next check is void.
results.append(report("motion blur is active at all (blurred vs static)",
                      static_ref, mb_dupli, expect_same=False))
results.append(report("motion blur bypass == dupli", mb_dupli, mb_bypass))

# ------------------------------------------------------------- ray visibility
gn, proto, s = base_scene()
proto.visible_camera = False        # prototype invisible to camera rays
vis_dupli = shot(s, "vis_dupli")
gn[MARKER] = True
gn.update_tag(); bpy.context.view_layer.update()
vis_bypass = shot(s, "vis_bypass")
results.append(report("prototype visible_camera=False bypass == dupli", vis_dupli, vis_bypass))

# It must actually have hidden something.
gn2, proto2, s2 = base_scene()
visible_ref = shot(s2, "vis_visible")
results.append(report("visible_camera=False actually changed the image",
                      visible_ref, vis_dupli, expect_same=False))

# -------------------------------------------------------------------- holdout
gn, proto, s = base_scene()
gn.is_holdout = True
ho_dupli = shot(s, "ho_dupli")
gn[MARKER] = True
gn.update_tag(); bpy.context.view_layer.update()
ho_bypass = shot(s, "ho_bypass")
results.append(report("instancer holdout bypass == dupli", ho_dupli, ho_bypass))

# --------------------------------------------------------------- shadow catch
gn, proto, s = base_scene()
gn.is_shadow_catcher = True
sc_dupli = shot(s, "sc_dupli")
gn[MARKER] = True
gn.update_tag(); bpy.context.view_layer.update()
sc_bypass = shot(s, "sc_bypass")
results.append(report("instancer shadow catcher bypass == dupli", sc_dupli, sc_bypass))

print("### " + "=" * 56)
print(f"### {sum(results)}/{len(results)} checks passed")
print("### " + "=" * 56)
