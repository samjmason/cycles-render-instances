# Scoping: true nested instancing in Cycles (OptiX)

**Question.** Could Cycles be extended so a "tree" of 100k leaf-instances is
stored **once** and instanced 50k times — the way Houdini packed prims, Arnold
procedurals, RenderMan, and V-Ray proxies work — instead of flattening to one
Cycles object per final leaf (5 billion objects for 50k×100k)?

**Verdict.** The acceleration-structure plumbing is cheap. The blocker is that
Cycles' object identity is a single flat integer assumed *pervasively* across
the shading kernel. Adding real nesting is a multi-subsystem, kernel-touching
rewrite — an upstream-scale core-engine project, not a patch. The OptiX-only
constraint helps (removes four of five backend reimplementations) but does not
remove the core kernel object-model rework.

Based on a three-part source investigation of Blender 5.2 Cycles.

---

## Confirmed first: stock Cycles really does flatten (measured)

Cycles' own object count (`scene->objects.size()`, not the depsgraph), untagged
nested forest, leaves fixed at 100/tree:

| trees × leaves | Cycles objects |
|---|---|
| 25 × 100 | 2,628 |
| 100 × 100 | 10,203 |
| 400 × 100 | 40,503 |

Scales as the **product**, linear in tree count. Stock Cycles creates one
`Object` per final leaf. The leaf *mesh* is shared (one `Geometry`), but every
leaf-in-every-tree is a separate object record. At 50k×100k that is 5 billion
object records — infeasible on memory, and (see below) beyond the hardware ID
space regardless of memory. "Nested instances work" in Blender means *the image
is correct*, not *the tree is a shared memory unit*.

---

## 1. OptiX build side — cheap, reusable (not the blocker)

`kernel/device/optix/device_impl.cpp`. Today Cycles builds one flat IAS
(`OPTIX_BUILD_INPUT_TYPE_INSTANCES`, one `OptixInstance` per object, `:1929`),
each instance's `traversableHandle` pointing at a per-geometry GAS (`:1776`).

- `build_optix_bvh` (`:1280`) is already generic over `OptixBuildInput`; a
  per-group IAS is just another instances-build. **Reusable as-is.**
- `BVHOptiX::traversable_handle` (`bvh/optix.h:22`) is an opaque handle that
  could hold a group-IAS handle. **No structural obstacle.**

Three real but bounded gotchas:
- **Pipeline forbids nesting by default.** `traversableGraphFlags =
  ALLOW_SINGLE_LEVEL_INSTANCING` in the common case (`:392`); only motion blur
  flips it to `ALLOW_ANY` (`:414`). Nesting requires `ALLOW_ANY` always — which
  the code itself notes is the slower option.
- **`sbtOffset` sums down the instance path.** Cycles uses it as a geometry-type
  tag (`:1808`), so outer "tree" instances must be a distinct class contributing
  offset 0.
- **Instance-ID ceiling.** Cycles queries `MAX_INSTANCE_ID`, reserves the top
  bit, and errors past it (`:1735`). The flat model wants 5B IDs — beyond the
  hardware space. Nesting is what *avoids* that (≈150k IDs: 100k leaves + 50k
  trees), but only once the kernel can address a hit by (tree, leaf) instead of
  one flat id.

Estimated build-side change: a few hundred lines. **This is the easy part.**

---

## 2. Kernel object identity — THE WALL

`kernel/`. On a hit, `get_object_id()` (`device/optix/bvh.h:41`) deliberately
reads only the **top-level** instance id and is hard-wired to a two-level
layout. That single `int` becomes `Intersection.object` → `ShaderData.object`
and indexes ~a dozen flat device arrays for **all** per-instance state:

- transform / inverse transform (`geom/object.h:33`)
- attributes (`geom/attribute.h:37,107`) — including per-instance attributes
- random_id, color, pass_id, lightgroup, holdout, visibility, cryptomatte,
  velocity, AO distance, shadow-terminator offsets (`KernelObject`,
  `types.h:1412`)
- motion transforms (`geom/object.h:56`)
- light linking / shadow linking, self-intersection skipping

**Nothing anywhere composes two transforms.** The nested effective transform
(tree × leaf) does not exist in the model.

To shade a nested hit you must thread a **path** (≥2 ids) through
`Intersection`, `ShaderData`, and every one of those consumers, and add
transform composition on the hot intersection path. Registers are already fully
allocated ("locked", `bvh.h:6`), so this widens the intersection ABI. In stock
Cycles this is **replicated across five backends** (OptiX, CPU/Embree, HIP-RT,
Metal, oneAPI).

Good news that survives: the **shader id and triangle vertex/attribute data are
keyed off global prim + a shared vertex array**, so geometry sharing already
works and would carry over. The hard part is exclusively the *per-instance*
state.

**Estimate: a multi-month rewrite of the object/shading data model.** This is
the cost centre. It interacts worst with motion blur (which already uses the
>2-level graph path).

---

## 3. Host / scene model — no group concept, 1:1 bijection

`scene/`. `Object` has exactly one `Geometry` and one `Transform`
(`object.h:41,43`); there is **no** "object references a group of objects."
`ObjectManager` assigns a linear `index` and packs one `KernelObject` per object
(`object.cpp:922`, arrays sized `scene->objects.size()`). The two-level BVH is
stitched with **one `object_node` entry per object** (`bvh2.cpp:509`) — hard-wired
two-level, no slot for "object → group → members".

Net-new host work: a `Group`/`InstanceGroup` node owning its members + an
intermediate BVH; a group-instance object variant whose device record is a
lightweight `{group_id, tfm}`; KernelObject indirection breaking the 1:1
assumption (and an audit of every `ob->index`-keyed array); three-tier BVH
packing; motion composition. `Procedural` (`procedural.h:22`) can host the
*authoring* logic but does **not** provide a runtime shared sub-scene.

The host agent's own recommendation: **do not build nested instancing unless
flattening is proven insufficient at the target object counts** — flattening is
the intended Cycles idiom, and geometry is already deduped.

---

## What the OptiX-only constraint changes

Meaningful, but not decisive:
- **Removes four of five backend reimplementations** (CPU/Embree, HIP-RT, Metal,
  oneAPI) from the kernel change — a real reduction of the §2 surface area.
- The **OptiX build side** (§1) is already the cheap part.
- The **OptiX kernel object-model rework** (§2) remains, and it is the majority
  of the cost.

So OptiX-only turns "multi-month across five backends" into "multi-month on one
backend + host model." Better, still a major project, still changes the
intersection/shading ABI in a way that needs core-team design agreement to ever
land upstream.

---

## The pragmatic path that works **today**

Realize the leaves into the tree mesh (GN **Realize Instances**), then instance
the tree. Verified bit-identical to stock and **feasible at scale** (object count
= number of trees, not the product); this patch accelerates the tree scatter.

Trade-off vs true nesting — it shares the tree *mesh* but bakes the leaves, so it
stores more triangles:

| leaf poly | leaves/tree | realized tree mesh (stored once, shared) |
|---|---|---|
| 100 tri | 100k | 10 M tris |
| 100 tri | 500k | 50 M tris |
| 1000 tri | 100k | 100 M tris |
| 1000 tri | 500k | 500 M tris |

With a few shared hero trees this is fine at the low/mid end and heavy (tens of
GB) at the high end (500k high-poly leaves). True nested instancing would store
the leaf **once** per hero tree instead — that is precisely the saving that
justifies the §2 project, and only at that high end.

**Rule of thumb:** if your trees are a handful of hero variants with low/mid-poly
leaves, realize + instance is the answer now. If they are high-poly leaves at
100–500k per tree where the realized mesh itself blows memory, that is the case
true nesting is for — and there is no shortcut to it in Cycles' current
architecture.

---

## Recommendation

1. **Near term:** realize + instance; the patch already makes that fast and
   correct. Establish whether its memory is actually a problem for the real
   assets before investing further.
2. **True nesting is a core-Cycles feature, not a patch.** OptiX-only makes it
   more tractable but it still reworks the kernel object model and needs
   upstream design buy-in. It is the right thing to raise with a core Cycles
   developer — it is a known, wanted, unimplemented capability, not a fix.
3. Do **not** attempt to bolt nested IAS onto the current kernel: the build side
   would succeed and then produce wrong shading, because the object identity is
   a single flat index everywhere.
