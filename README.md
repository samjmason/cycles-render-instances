# Cycles render instances
VIBE CODED :(  :( :( :( :( :( :( :( :(

An experimental patch for Blender 5.2 that lets Cycles read geometry-nodes
instances directly instead of having them expanded through `object_duplilist()`.

The short version: a ~1M instance scatter goes from unusable to editable in the
Cycles viewport. On my machine, re-sync at 1M instances drops from ~3.6 s to
~60 ms, and camera navigation drops to roughly a millisecond of Cycles sync.

the code has rough edges and has been tested on exactly
one machine and one Blender version. Please treat it accordingly.

## The problem

Scattering ~1M instances with geometry nodes renders fine in Cycles but is not
editable. Any depsgraph change — moving the camera, selecting an object,
adjusting a scatter parameter — stalls the viewport for seconds. Other
production renderers handle this; Cycles currently does not, and nothing on the
roadmap addresses it.

There are two per-instance costs, both paid on every sync:

1. `object_duplilist()` expands instances into a `DupliObject` list. The
   depsgraph iterator calls it unconditionally and throws the list away
   afterwards, so there is nothing cached to reuse.
2. `BlenderSync::sync_object()` then runs its full body for every instance —
   `BObjectInfo` setup, culling, two RNA pointer lookups, attribute sync, and
   so on.

The second turns out to be the larger of the two.

## The approach

Geometry nodes has already built the instances in unrealized form. They sit in
`object->runtime->geometry_set_eval` as a `bke::Instances` with `transforms()`,
`reference_handles()` and `references()` — which is exactly what
`object_duplilist()` reads and expands. So Cycles reads that directly and builds
one object per instance sharing one geometry, skipping both costs.

Enable it per object with a custom property:

```python
scatter_object["cycles_render_instancer"] = True
```

It appears in Object Properties → Custom Properties as a checkbox. No new node,
no DNA/RNA change, and existing scatters do not need re-authoring.

## One thing worth knowing if you try something similar

I started out assuming this could be confined to `BlenderSync`. It cannot.

The depsgraph iterator drives dupli expansion, so detecting the instancer inside
Cycles' object loop and calling `continue` only skips Cycles' *handling* of
duplis that were already generated. The expansion is still paid, and the dupli
children (which carry no marker) then go through normal `sync_object()`.

The result was every instance being built twice — 10 002 Cycles objects became
20 001 — and the patch benchmarked *slower* than stock while producing a
pixel-identical render. It needs a small change in `depsgraph_query_iter.cc` to
stop the expansion at source, which is why this patch touches blenkernel and not
just `intern/cycles`.

## Results

Blender 5.2, CPU-only build, Threadripper 3970X. Plain geometry-nodes
`Instance on Points` scatter, tag off vs on, alternating within a single process,
best-of-3 re-syncs.

| instances | dupli path | this patch | |
|---|---|---|---|
| 490 k | 1643 ms | 346 ms | 4.7x |
| 1 M | 3591 ms | 61 ms | 58.7x |

Cycles sync alone at 1M on a camera-only move: 3.617 s → ~0.001 s, because an
untouched instancer skips its loop entirely. An edit that *does* change the
scatter still pays a full rebuild, around 660 ms at 1M. With two instancers in a
scene, editing one no longer forces the other to rebuild (813 ms → skipped).

Verified bit-identical to the dupli path (mean and max channel difference
`0.000000`) for plain scatters, collection instancing, nested instances, motion
blur, ray visibility, holdout, shadow catcher, materials, and instancer-source
Attribute nodes.

Absolute timings on this machine drift by ~3x with background load, so every A/B
figure above is an alternating measurement inside one process. Numbers compared
across separate sessions would not mean much.

## Caveats

Please read these before trusting it with anything real.

- **Tested on one machine, one Blender version** (5.2, tag `v5.2.0`,
  `fbe6228777e7`). No CI, no cross-platform testing.
- **CPU-only.** I never installed the CUDA toolkit, so the build has no GPU
  kernels and GPU rendering is unverified. The patch is confined to the sync
  layer and does not touch kernel or device code, so there is no obvious reason
  it would behave differently on GPU — but "no obvious reason" is not the same
  as tested.
- **Particle systems and legacy dupli paths** are untouched and untested.
- **Prototype-level object flags do not propagate** to geometry-nodes instances.
  This matches stock behaviour (instances reference anonymous geometry rather
  than the prototype object), but it can surprise you.
- The marker is a custom property rather than proper RNA, which is fine for
  experimenting and not what you would ship.

If you hit something, I would genuinely like to know — particularly on GPU, or
with scenes more complicated than the synthetic ones in `tests/`.

## Trying it

```bash
git clone https://projects.blender.org/blender/blender.git
cd blender
git checkout blender-v5.2-release
git apply /path/to/patches/0001-cycles-render-instances.patch
make update
make release
```

Then tag a geometry-nodes scatter with the custom property above and render or
open a Cycles viewport. `tests/` contains the benchmark and verification
scripts; each runs as `blender -b -P <script>`.


## Files touched

| file | what |
|---|---|
| `intern/cycles/blender/object.cpp` | reader, reference flattening, flat storage, lifecycle |
| `intern/cycles/blender/sync.h` | declarations, `RenderInstanceSet` |
| `intern/cycles/blender/sync.cpp` | per-instancer dirty tracking in `sync_recalc` |
| `source/blender/depsgraph/intern/depsgraph_query_iter.cc` | skip dupli expansion for tagged objects |

`docs/TECHNICAL.md` has the implementation detail with file:line references.
`docs/DEV-NOTES.md` is the working log, including the things I got wrong along
the way and had to retract.

## License

Blender is GPL-2.0-or-later, so this patch is too.
