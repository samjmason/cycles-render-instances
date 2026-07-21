# Cycles: reading geometry-nodes instances directly

Patch against Blender `blender-v5.2-release` (tag v5.2.0, `fbe6228777e7`).

## Problem

A geometry-nodes scatter of ~1M instances renders fine in Cycles but is not
editable. Any depsgraph change — moving the camera, selecting an object,
scrubbing a parameter — stalls the Cycles viewport for seconds.

Two costs, both per-instance and both paid on every sync:

1. `object_duplilist()` expands the instances into a `DupliObject` list. The
   depsgraph iterator calls it unconditionally and discards the list afterwards
   (`depsgraph_query_iter.cc:253`, `:164`), so there is no cache to invalidate.
   Measured ~86 ms/M steady state.
2. `BlenderSync::sync_object()` then runs its full body for every instance:
   `BObjectInfo` construction, `object_get_data`, culling, two RNA pointer
   lookups, `ao_distance`, caustics/shadow-catcher flags, `sync_object_attributes`.

Cost 2 is the larger of the two.

## Approach

Geometry nodes has already built the instances in unrealized form:
`object->runtime->geometry_set_eval` holds a `bke::Instances` with
`transforms()`, `reference_handles()` and `references()`. That is precisely the
input `object_duplilist()` expands. Cycles reads it directly and builds one
`Object` per instance sharing one `Geometry`, skipping both costs.

Opt-in per object via a custom property, `cycles_render_instancer = True`. No
DNA/RNA change, no new node, no re-authoring of existing scatters.

## The part that isn't confined to Cycles

The brief for this work assumed a patch scoped to `BlenderSync`. That does not
work. The depsgraph iterator drives dupli expansion, so detecting the instancer
in Cycles' object loop and calling `continue` skips only Cycles' *handling* of
duplis that were already generated. The expansion is still paid, and the dupli
children — which carry no marker — then flow through normal `sync_object()`.

Result: every instance built twice. Cycles scene object count at 10 000
instances went 10 002 (correct) → 20 001. Benchmarked at 0.56x, i.e. slower
than stock.

The fix is in `depsgraph_query_iter.cc`: skip `object_duplilist()` for objects
carrying the marker. About 15 lines, but it means the patch spans blenkernel,
not just `intern/cycles`.

## Implementation notes

**Reference flattening.** One reference can draw several things, so references
flatten into a list of (geometry, local transform):
- `Type::Object` → the object's geometry.
- `Type::GeometrySet` → its mesh, and/or nested instances, recursed with a
  depth cap of 8 (`MAX_DUPLI_RECUR`).
- `Type::Collection` → each visible member. Transform math taken from
  `make_duplis_collection` (`object_dupli.cc:522`):
  `collection_mat = instance_matrix * translate(-instance_offset)`, then
  `member = collection_mat * member_object_matrix`. Visibility mirrors
  `FOREACH_COLLECTION_VISIBLE_OBJECT_RECURSIVE`.

Note geometry nodes never emits `Type::Object`. `Object Info → Instance on
Points` always yields `GeometrySet`, with or without "As Instance". `Object`
references come from legacy dupli and particles.

**Storage.** Instance objects live in a flat `vector<Object *>` per instancer,
indexed by instance, not in `object_map`. `id_map` wraps an ordered
`std::map` with no locking; at 1M instances the per-instance lookup was 1825 ms
of a 2266 ms loop (80%) on re-sync, where no allocation happens at all — pure
red-black tree traversal with poor cache behaviour. Flat indexing dropped that
to 27.8 ms.

These objects are outside `id_map`'s sweep, so lifecycle is explicit:
`render_instances_pre_sync()` / `render_instances_post_sync()`, the latter
running before `object_map.post_sync()` to preserve the objects-before-geometries
delete ordering.

**Per-instancer dirty check.** `sync_objects()` walks every object, so without
a dirty check, editing instancer B re-runs instancer A's entire loop (813 ms at
490k). `sync_recalc` now records instancers tagged with
`ID_RECALC_GEOMETRY|TRANSFORM|SHADING`; untagged instancers skip their loop.

The skip must sit **after** prototype resolution, not at function entry.
Skipping everything means `sync_geometry` is never called for the prototype, so
`geometry_map.post_sync()` frees it while every instance object still holds the
pointer. Only the O(N) loop is skipped; the O(num_references) resolution still
runs.

**Motion blur.** `sync_objects()` is re-run once per motion step. Ignoring
`motion_time` meant each step rebuilt the instances at that step's transforms —
scatter in the wrong position, no motion recorded, no error. Motion passes now
write into existing objects' motion arrays via `set_motion_tfm`; center time
sizes them with `sync_object_motion_init`.

**Materials.** `sync_geometry` resolves shaders via
`find_used_shaders(b_ob_info.iter_object)` (`geometry.cpp:112`), reading the
*object's* material slots. The instancer has none, so anonymous geometry-nodes
geometry silently rendered with `default_surface`. Materials for that geometry
live on the mesh, so `used_shaders` is built from `mesh->mat[]` (honouring
`view_layer.material_override`) and set on the geometry. Slot order matches, so
per-face shader indices stay valid.

**Instancer attributes.** `BKE_object_dupli_find_rgba_attribute` needs a
`DupliObject`, but `find_geonode_attribute_rgba` (`object_dupli.cc:1942`) only
does `component->attributes()->lookup<ColorGeometry4f>(name)[instance_idx]`.
Both operands are already available, so the attribute is read directly.
Gathered once per sync from `geometry->needed_attributes()` filtered to
`SHD_ATTRIBUTE_INSTANCER`; scenes not using them pay nothing.

**Other flags.** visibility (`prototype & instancer`), holdout, shadow catcher,
indirect-only, `ao_distance`, caustics caster/receiver, shadow-terminator
offsets — all derived as `sync_object()` does for a dupli, with the instancer
as parent.

## Results

Blender 5.2 CPU-only build, Threadripper 3970X. Plain geometry-nodes
`Instance on Points` scatter, tag off vs on, alternating in one process,
best-of-3 re-syncs.

| N | dupli path | bypass | |
|---|---|---|---|
| 490 k | 1643 ms | 346 ms | 4.7x |
| 1 M | 3591 ms | 61 ms | 58.7x |

Cycles sync alone at 1M on a camera-only move: 3.617 s → ~0.001 s, because the
dirty check skips the loop entirely. An edit that does touch the scatter pays a
full rebuild, ~660 ms at 1M. Two instancers, editing one: the other goes from
813 ms to skipped.

Verified bit-identical to the dupli path (mean and max channel difference
`0.000000`) for: plain scatters, collection instancing, nested instances,
motion blur, ray visibility, holdout, shadow catcher, materials, instancer
attributes.

Absolute timings on this machine drift ~3x with background load, so all A/B
numbers are alternating measurements within a single process. Comparing against
a figure from an earlier session is meaningless.

## Testing notes

Two failure modes wasted significant time and are worth stating plainly.

**A pixel-identical render can mean nothing.** While the patch was building
every instance twice, the render was bit-exact — two complete, perfectly
overlapping copies of a scatter match. Mean and max difference were `0.000000`
while the patch was doing double work and running slower than stock. Cycles
object count caught it immediately. Check what produced the image before
comparing images.

**A parity test where both sides are trivially identical proves nothing.**
Materials were broken for hours behind a passing test, because every test scene
used the default material on both sides: grey == grey matches as convincingly
as red == red. Every feature check now asserts twice — that the feature visibly
changes the image at all, and that bypass matches dupli. The first assertion is
what makes the second meaningful.

## Known gaps

- `Attribute` nodes with **Object** source on GN instances are untested;
  Instancer source works.
- Prototype-level object flags do not propagate to GN instances — correct, since
  instances reference anonymous geometry, not the prototype object, but worth
  knowing.
- Particle systems and legacy dupli paths are unchanged and untested here.
- Viewport interactivity has been confirmed by hand but not measured; all
  numbers above are headless sync timings.
- CPU-only build. No CUDA/OptiX kernels were compiled, so final-render parity on
  GPU is unverified.

## Files

- `intern/cycles/blender/object.cpp` — reader, flattener, storage, lifecycle
- `intern/cycles/blender/sync.h` — declarations, `RenderInstanceSet`
- `intern/cycles/blender/sync.cpp` — instancer dirty tracking in `sync_recalc`
- `source/blender/depsgraph/intern/depsgraph_query_iter.cc` — dupli-expansion skip
