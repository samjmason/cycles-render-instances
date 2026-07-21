# Cycles "Render Instances" — verified source & baseline notes

> **STATUS: Tier 1 acceptance MET. At 1M instances Cycles sync drops
> 3.617 s -> 0.183 s (19.7x); wall re-sync 3673 ms -> 580 ms (6.34x).
> Verified pixel-identical with correct object counts.**
>
> Read §00 first — the brief's central architectural claim is wrong, and that
> error silently corrupted every measurement taken before it was found.

---

## 000. Results (flat storage + depsgraph dupli-skip)

Plain GN `Instance on Points` scatter, one custom property ticked. Same binary,
alternating tag off/on in one process, best-of-3 re-syncs.

| N | metric | dupli path | bypass | speedup |
|---|---|---|---|---|
| 490 k | wall re-sync | 1643 ms | 346 ms | **4.74x** |
| 1 M | wall re-sync | 3673 ms | 580 ms | **6.34x** |
| 1 M | **Cycles sync only** | 3.617 s | **0.183 s** | **19.7x** |

Our loop at 1M: **184 ms** (object_map 27.8 ms / 15%, build 156 ms).
Before flat storage it was 2266 ms with object_map at 1825 ms / 80%.

Correctness: renders bit-identical (mean & max diff `0.000000`), object count
stable at N+1 across repeated syncs (no leak, no double-create).

### ⚠️ Retraction

An earlier revision of this file claimed sub-second at 1M was unreachable
because ~2.5 s sat in `ObjectManager::device_update` + BVH, "not reachable from
the sync layer". **That was wrong.** It was measured on the build that was
still expanding duplis *and* running our loop (see §00), so most of that
"downstream" term was in fact the duplilist expansion. With the real bypass it
largely disappears. Sub-second at 1M is comfortably achieved.

Lesson: a cost attributed to "somewhere downstream, unfixable" deserves the
same suspicion as any other unmeasured claim.

### Multi-instancer independence (per-instancer dirty skip)

Two instancers, A = 490 k, B = 10 k, moving **only B**:

| | before | after |
|---|---|---|
| A's loop | 813 ms (full rebuild) | **skipped, ~0** |
| B's loop | 13 ms | 2.7 ms |
| object count | — | 500 001, stable ✅ |

Render after a skip is bit-identical to the dupli reference, with both scatters
present.

⚠️ **The skip must come AFTER prototype reference resolution.** Skipping the
whole function means `sync_geometry` is never called for the prototype, so
`geometry_map.post_sync()` frees it and every instance object is left holding a
dangling `Geometry *`. Only the O(N) instance loop is skipped; the
O(num_references) resolution still runs. Guards: not in `render_instances_recalc`,
instance count unchanged, count > 0.

---

## 00. THE KEY FINDING — the bypass cannot live in Cycles alone

Brief §3 claims this is a scoped patch inside `BlenderSync`. **That is wrong,
and it silently broke every measurement taken before this was found.**

`BlenderSync::sync_objects` iterates via `DEG_iterator_objects_*`. Detecting the
instancer there and calling `continue` does **not** prevent dupli expansion —
`DEG_iterator_objects_next` calls `object_duplilist()` when it advances past an
instancer (`depsgraph_query_iter.cc:253`), *before* the engine sees anything.
`continue` only skips Cycles' handling of each already-generated dupli. The
dupli children carry no marker, so they then flow through normal `sync_object()`.

**Proof (Cycles scene object count, 100x100 grid = 10 000 instances):**

| | objects in Cycles scene |
|---|---|
| tag off (correct) | 10 002 |
| tag on, engine-only patch | **20 001** ← every instance built twice |
| tag on, + depsgraph patch | 10 001 ✅ |

Consequences that invalidated earlier work:
1. The engine-only patch **never avoided the duplilist cost at all** — it paid
   expansion *plus* its own loop. That is why it benchmarked at **0.56x**
   (slower than baseline), not because of any cost model subtlety.
2. **A pixel-identical render proved nothing.** Two complete, perfectly
   overlapping copies of the scatter produce a bit-exact match. Mean and max
   diff were `0.000000` while the patch was doing double work.
   **Verify instance/object COUNT before comparing images.**

### The fix (this is why the patch spans blenkernel, not just Cycles)

`depsgraph_query_iter.cc` — skip expansion for marked objects, since only the
iterator can prevent it:

```cpp
static bool deg_object_skips_dupli_expansion(const Object *object);   /* reads the marker IDProperty */

if ((data->flag & DEG_ITER_OBJECT_FLAG_DUPLI) &&
    ((object->transflag & OB_DUPLI) || object->runtime->geometry_set_eval != nullptr) &&
    !deg_object_skips_dupli_expansion(object))
{ ... object_duplilist(...); }
```

This is a narrow version of the "big change to the Blender API" Brecht cited —
one condition rather than an iterator restructure — but **upstreaming now
touches the depsgraph**, which the brief did not anticipate.

### Verified result after the fix (490 000 instances, plain GN scatter)

```
dupli path : 3137.0 ms      piggyback : 1907.4 ms      SPEEDUP 1.64x
```
- object count 490 001 vs baseline 490 002 (the GN object itself has no
  realized geometry of its own) ✅
- render bit-identical, mean/max diff 0.000000, 40 000 non-black both ✅

⚠️ Transient: toggling the tag *mid-session* shows one render at 980 002 objects
while the previous dupli objects await GC. Steady state is correct.

---

## 0a. Collections + nested instances (DONE)

All three reference types now supported. A reference flattens into a **list** of
(geometry, local transform) pairs, because one reference can draw several
things; the loop emits one Cycles object per (instance x piece).

`render_instances_collect_prototypes()` in object.cpp, recursive, depth-capped
at 8 (Blender's `MAX_DUPLI_RECUR`), logs on truncation:
- **Object** -> the object's geometry, identity local
- **GeometrySet** -> its mesh, and/or nested instances (recursed)
- **Collection** -> each visible member. Transform math copied from
  `make_duplis_collection` (`object_dupli.cc:522`):
  `collection_mat = instance_matrix * translate(-instance_offset)`, then
  `member = collection_mat * member_object_matrix`.
  Visibility mirrors `FOREACH_COLLECTION_VISIBLE_OBJECT_RECURSIVE`:
  `BASE_ENABLED_VIEWPORT/RENDER` + `OB_HIDE_VIEWPORT/RENDER`, chosen by
  `preview`. `BKE_collection_object_cache_get()` is already flattened over
  nested collections.

Verified:
| case | objects | vs dupli baseline | pixels |
|---|---|---|---|
| collection of 3 members x 400 pts | **1200** (3x, correct) | 1204 vs 1205 | identical |
| nested instances ("As Instance") | 900 -> 900, 0 empty | 902 vs 903 | identical |

(The consistent 1-object gap is the GN object itself, which has no realized
geometry of its own.)

### Latest headline numbers, 1M instances

```
dupli path : 4176 ms      bypass : 82 ms      SPEEDUP 50.9x
Cycles sync on camera-only move: 0.0012 s   (loop skipped entirely)
```
The jump from the earlier 19.7x is the per-instancer dirty skip: moving the
camera does not tag the instancer, so the whole 1M loop is skipped and sync
collapses to ~1 ms. A change that *does* touch the instancer still pays the
full rebuild (~970 ms at 1M).

---

## 0a2. Production parity: motion blur, visibility, holdout (DONE)

**Motion blur was an active bug, not just a missing feature.** `sync_objects()`
is re-run once per motion step, and the instance path ignored `motion_time`, so
every step *rebuilt* the instances at that step's transforms — leaving the
scatter at the wrong position with no motion recorded. Silent wrong render.

Fixed: `sync_render_instances` now takes `motion_time`. When non-zero it finds
the existing objects and writes `set_motion_tfm(tfm, motion_step(motion_time))`,
never rebuilding; center-time sizes the arrays via `sync_object_motion_init`.
The dirty-skip is bypassed during motion passes.

Flags now mirrored from `sync_object()`, with the instancer as dupli parent:
`visibility = prototype & instancer` (plus indirect-only clearing camera),
holdout (`BASE_HOLDOUT` / `OB_HOLDOUT`), shadow catcher (prototype **or**
instancer), `ao_distance`, caustics caster/receiver, both shadow-terminator
offsets. Per-prototype visibility/shadow-catcher is captured during flattening,
so collection members keep their own flags.

Materials needed no work — they ride on the `Geometry`, and collection members
sync through their own `BObjectInfo`, so per-member slots already apply.

### Verified

| check | result |
|---|---|
| motion blur active (blurred vs static) | differs ✅ |
| motion blur bypass == dupli | identical ✅ |
| instancer `visible_camera=False` propagates | differs ✅ |
| instancer `visible_camera=False` bypass == dupli | identical ✅ |
| instancer holdout bypass == dupli | identical ✅ |
| instancer shadow catcher bypass == dupli | identical ✅ |

Test design note: each feature asserts **twice** — that the feature changes the
image at all, *and* that bypass matches dupli. Without the first assertion a
test passes when the feature is a no-op in both paths, which is exactly the
trap that made the double-render look correct (§00).

Two "feature changed the image" assertions came back identical and are
**vacuous tests, not bugs**: prototype-level flags legitimately do not
propagate to GN instances (they reference anonymous geometry, not the
prototype object) in *either* path, and `visible_shadow` had no visible shadow
in that framing.

### Instancer Attribute nodes — SUPPORTED

`BKE_object_dupli_find_rgba_attribute` needs a `DupliObject`, but following it
down to `find_geonode_attribute_rgba` (`object_dupli.cc:1942`) it only does:

```cpp
component->attributes()->lookup<ColorGeometry4f>(name)[dupli->instance_idx]
```

i.e. read the named attribute off the instances component at the instance
index — both of which we already have. So we read it directly rather than
synthesising a fake DupliObject.

Gathered once before the loop from `geometry->needed_attributes()`, filtered to
`SHD_ATTRIBUTE_INSTANCER`; the VArray is fetched once per attribute, only the
indexed read is per-instance. If no shader requests one, the per-instance
branch never runs, so unaffected scenes pay nothing.

Verified: attribute genuinely varies the image (mean 0.163, max 0.796) **and**
bypass == dupli identical.

### ⚠️ MATERIALS WERE BROKEN — and an earlier revision of this file said they were fine

`sync_geometry` resolves shaders via `find_used_shaders(b_ob_info.iter_object)`
(`geometry.cpp:112`), reading the **object's** material slots. We pass the
instancer, which has none, so anonymous GN geometry silently rendered with
`default_surface`. The dupli path is fine because `object_duplilist` builds a
temporary object wrapping the generated mesh.

Measured before the fix (red prototype material): dupli `R=0.090 G=0.030`
(red), bypass `R=0.083 G=0.083` (**grey**).

Fix: anonymous geometry's materials live on the mesh, so build `used_shaders`
from `b_mesh->mat[0..totcol)` (honouring `view_layer.material_override`) and
`geometry->set_used_shaders(...)`. Slot order matches, so per-face shader
indices stay valid. After: bit-identical, `R=0.090 G=0.030` both sides.

**This also caused the instancer-attribute failure** — with no material, the
shader graph carrying the Attribute node was never attached, so
`needed_attributes()` returned nothing. One root cause, two symptoms.

**Why it went unnoticed:** every earlier test used the *default* material on
both sides, so grey == grey matched perfectly and read as confirmation. Same
class of error as §00's double-render.
**Rule: a parity test where both sides are trivially identical proves nothing.
The feature under test must be visibly doing something first.**

Post-fix regression: **58.7x** at 1M, collections still bit-identical.

Post-parity regression check, 1M: **58.4x** (3486 ms -> 59.7 ms). No regression.

---

## 0b. Reference types: GN never produces Object references

`Object Info -> Instance on Points` yields a **`GeometrySet`** reference, with or
without "As Instance". `Type::Object` appears to come from legacy dupli /
particles, not geometry nodes. The `Object` branch implemented first was nearly
useless; `GeometrySet` is the case that matters.

Resolution: `BObjectInfo` explicitly supports ownerless geometry — `object_data`
"might have a different type compared to `object_get_data(real_object)`"
(`util.h:55-78`). So point it at the mesh inside the geometry set:
```cpp
BObjectInfo b_proto_info{&b_ob, &b_ob, const_cast<blender::ID *>(&b_mesh->id), false};
geom_by_handle[h] = sync_geometry(b_proto_info, false, false, nullptr);
```
Still unhandled: nested instances (GeometrySet containing Instances) and
Collection references — both logged explicitly, never silently dropped.

⚠️ `sync_geometry`'s `object_updated` must be **false**. Passing `true` forces a
prototype geometry rebuild + BVH rebuild every sync.

---

## 0. M0 first results — the bottleneck is NOT where the brief assumed

The patch triggers and builds instances from point data. Log line from the
patched build (`--debug-cycles`):

```
render-instances: 1000000 instances in 3144.87 ms (object_map 2788.04 ms, 88.65%; build 356.87 ms)
```

Scaling of the **cold** sync:

| N | total | object_map | build (attrs + transforms) |
|---|---|---|---|
| 10 k | 24.0 ms | 20.7 ms (86.4%) | 3.3 ms |
| 100 k | 271.7 ms | 237.2 ms (87.3%) | 34.5 ms |
| 1 M | **3144.9 ms** | 2788.0 ms (88.7%) | 356.9 ms |

**Reading this correctly matters.** `object_map.add_or_update` is ~88% of cold
sync, but that is *not* overhead the patch introduces — it is
`create_node<Object>()` heap-allocating one Cycles `Object` node per instance,
and **the GN dupli baseline pays exactly the same cost through the same call**
(`object.cpp:269`). It is shared, not differential.

So the honest framing of cold sync at 1M is:

- shared, unavoidable: ~2.8 s allocating 1M Object nodes
- differential (what we remove): the `object_duplilist` expansion, ~86–250 ms
  depending on machine load

i.e. **bypassing duplilist saves only a few percent of a *cold* sync.** If the
goal were cold-sync time, this project would be close to pointless.

### Why the project is still sound: cold sync is the wrong metric

The brief's actual complaint is the *interactive* stall — select the camera,
scrub a parameter, and wait. That is a **re-sync**, not a cold sync. On re-sync
`id_map::add_or_update` finds the existing Object and **skips allocation
entirely** (`id_map.h:104-118`), so the 2.8 s term largely disappears and what
remains is:

- baseline: `object_duplilist` expansion + N map lookups
- bypass:   N map lookups (no duplilist)

**That** is where the differential should show up, and it is the number that
decides Tier 1. Measured with `render.use_persistent_data = True`, which keeps
the Cycles session and its `object_map` alive between renders
(`scratchpad/bench_resync.py`).

⚠️ Do not quote the 3.1 s figure as "the patch is slow" or the cold-sync
comparison as a win/loss — it is dominated by a cost both paths share.

### Cold full-render A/B at 1M (same binary, same process, alternating)

```
baseline (GN dupli)    : 15960.6 ms
bypass   (point cloud) : 10843.2 ms
SPEEDUP                :     1.47x
```

The bypass saves **5.1 s**, which is ~20x more than the `object_duplilist`
expansion alone (~250 ms under this load). So the win is **not** mainly the
dupli expansion — it is skipping the whole heavyweight per-instance
`sync_object()` body that the dupli path runs for every one of the 1M
instances: `BObjectInfo` construction, `object_get_data`, culling test,
`RNA_id_pointer_create` + `RNA_pointer_get("cycles")`, `get_float(ao_distance)`,
caustics/shadow-catcher flag reads, and `sync_object_attributes`
(`object.cpp:163-360`).

This confirms the brief's §2 claim ("re-packs one heavyweight Cycles Object per
instance") as the dominant term, and corrects its emphasis: the dupli iterator
is the *smaller* half of the problem.

### M0 VERDICT — sync-isolated re-sync at 1M

Using Cycles' own `Total time spent synchronizing data` (excludes BVH build and
sampling), with `use_persistent_data`, best-of-3 re-syncs:

| | cold sync | **re-sync (the stall)** |
|---|---|---|
| baseline (GN dupli) | 11.45 s | **8.80 s** |
| bypass (point cloud) | 6.63 s | **4.80 s** |
| **speedup** | 1.73x | **1.83x** |

End-to-end render: 1.41–1.47x.

**GO on the mechanism. MISS on the brief's acceptance target.**

Tier 1 acceptance (§10) asked for the 1M stall to drop from seconds to
**sub-second**. Actual: 8.80 s → 4.80 s. Directionally right, ~5x short.

#### Where the remaining 4.80 s sits

Our instrumented loop on **re-sync** (no allocation — objects already exist):

```
render-instances: 1000000 instances in 2266 ms (object_map 1825 ms, 80.5%; build 441 ms)
```

So of the 4.80 s sync:
- **1.83 s — `object_map` lookups.** With allocation gone, this is *pure*
  `std::map` find cost: ~1.8 µs per lookup over a 1M-node red-black tree, all
  cache misses. This is the risk flagged in §4, now confirmed and quantified.
- **0.44 s — actual useful work** (attribute reads + transform build + socket
  sets). This is the only part that is intrinsic to what we're trying to do.
- **~2.5 s — downstream Cycles work outside our loop**, i.e.
  `ObjectManager::device_update` packing 1M objects into device arrays, plus
  BVH work. Not reachable by any sync-side change.

#### Consequences for the plan

1. **Replacing `object_map` with flat per-instancer storage is now the single
   highest-value change** — worth ~1.8 s of 4.8 s, and it was already
   identified in §4 before any code was written. Do this next.
2. Even with that done, ~3.0 s remains at 1M, ~2.5 s of it downstream and
   untouchable from the sync layer. **Sub-second at 1M is not reachable via the
   Tier 1 approach alone.** The brief's acceptance number was set without
   knowledge of the device-update term.
3. Realistic revised targets: ~3 s at 1M after flat storage (≈2.9x vs
   baseline); sub-second plausibly reachable only at ~100–300k instances, or by
   also attacking `ObjectManager::device_update` — a substantially larger
   project touching the scene layer, not the sync layer.

Checkout: `D:\blender-git\blender` @ `fbe6228777e7` (tag **v5.2.0**, branch `blender-v5.2-release`).
Matches the installed production Blender 5.2 LTS (same hash `fbe6228777e7`).

---

## 1. Baseline measurements (stock Blender 5.2, headless)

Harness: `scratchpad/bench_baseline.py`, `scratchpad/bench_invalidation.py`.
Run headless (`blender -b --factory-startup -P ...`) so **no GL/draw path is involved** — this
isolates the blenkernel dupli cost, and sidesteps the known OpenGL instability on this box.

Scene: one GN object, Mesh Grid 1000×1000 → Instance on Points → Object Info(cube prototype).

| measurement | 1M instances |
|---|---|
| cold build (incl. GN cook + grid) | 148–210 ms |
| steady-state re-expansion | **~82–90 ms (~86 ms/M)** |
| after "select camera" poke | ~86 ms |
| after real camera move | ~86 ms |
| Blender RSS | 0.22 → 0.50 GB |

Magnitude agrees with the brief's 77–93 ms/M.

### ⚠️ Correction to the brief's stated mechanism

The brief says *"Selecting the camera invalidates the whole list."* **That is not the mechanism.**

Re-accessing the instance list with **nothing changed at all** costs the same ~86 ms as accessing it
after a camera select or a camera move. There is no cached dupli list to invalidate.

Confirmed in source — `source/blender/depsgraph/intern/depsgraph_query_iter.cc`:
- **line 253**: `object_duplilist(data->graph, object, data->settings->included_objects, data->dupli_list);`
  — the iterator calls `object_duplilist` per instancer object, every time.
- **line 164**: `data->dupli_list.clear();` — and throws the list away when done.

So the expansion is **unconditional and per-iteration**, not invalidation-driven.

**Why this strengthens the project rather than weakening it:** no amount of smarter change-detection
on the Blender side would avoid this cost, because there is nothing being cached. Cycles pays the
full N-instance expansion on *every* sync unconditionally. Bypassing it is the only fix — which is
exactly what this patch does.

**Caveat carried forward:** the timings above are via the Python RNA iterator. The C++ path Cycles
uses is the same underlying `DEG_iterator_objects_*` machinery (see §2), so the same
`object_duplilist` call is on Cycles' path — but the *timings* have not yet been directly measured
inside a Cycles sync. Do that once the patched build exists.

---

## 2. ⚠️ The brief's assumed Cycles API is out of date

The brief states BlenderSync "builds its objects by walking `BL::Depsgraph::object_instances`".
**In 5.2 that is wrong.** `object_instances` does not appear anywhere in `intern/cycles/`.

Cycles 5.2 has dropped the old `BL::` RNA-wrapper layer and uses **native `blender::` types and
direct blenkernel/DNA access**. The real loop, `intern/cycles/blender/object.cpp:463`:

```cpp
void BlenderSync::sync_objects(blender::Depsgraph &b_depsgraph,
                               blender::bScreen *b_screen,
                               blender::View3D *b_v3d,
                               const float motion_time)
```

which iterates (object.cpp:504–517):

```cpp
blender::DEGObjectIterSettings deg_iter_settings{};
deg_iter_settings.depsgraph = &b_depsgraph;
deg_iter_settings.flags = DEG_OBJECT_ITER_FOR_RENDER_ENGINE_FLAGS;
blender::DEGObjectIterData deg_iter_data{};
...
ITER_BEGIN (blender::DEG_iterator_objects_begin,
            blender::DEG_iterator_objects_next,
            blender::DEG_iterator_objects_end,
            &deg_iter_data, blender::Object *, b_ob)
```

**This is good news.** Native access means we can read evaluated point data directly without an RNA
round-trip, and there is already a `TaskPool geom_task_pool` (object.cpp:469) for threaded geometry
sync to imitate.

### Insertion point for the patch

Inside the `ITER_BEGIN` loop in `sync_objects`, before the `sync_object(...)` call at object.cpp:537.
Detect the marker custom property on `b_ob`, dispatch to `sync_render_instances(...)`, then
`continue` so the point cloud is not also synced as ordinary geometry.

### Relevant facts from `sync_object` (object.cpp:163+)

- `const bool is_instance = b_deg_iter_data.dupli_object_current;` (line 172) — instance-ness comes
  from the iterator's current dupli, which our path will not have. We must supply equivalents
  explicitly.
- Persistence key (line 244): `const ObjectKey key(b_parent, persistent_id, b_ob_info.real_object, use_particle_hair);`
  where `persistent_id = b_deg_iter_data.dupli_object_current->persistent_id` (line 184).
  **For Tier 2 we need to synthesize stable unique keys per point index** — the exact width/bound of
  `persistent_id` must be checked before relying on it.
- Update test (line 269): `object_map.add_or_update(&object, &b_ob.id, &b_parent->id, key) || !object->tfm_equals(tfm)`
  — note it already short-circuits on transform equality, relevant to Tier 2 refit.
- Per-instance parity work we must re-derive explicitly (since we skip the iterator): holdout
  (284), visibility (286), shadow catcher (288), AO distance (301), caustics flags (303–307),
  `sync_object_attributes` (279).

---

## 3. Build environment

- VS 2022 **Community**, bundled CMake at
  `C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin`
  — **must be added to PATH**, `make.bat` exits with "Cmake not found in path" otherwise.
- `vswhere.exe` is missing from PATH; `make.bat` falls back to autodetection and finds VS2022 fine.
- Invoking `make.bat`: use an **absolute path**, and from Git-Bash use `cmd.exe //c` (a single `/c`
  gets mangled into a path by MSYS). `make.bat update` **prompts interactively** to download the
  library set — pipe `(echo y)` into it.

`make.bat update` prompts interactively and piping `y` does **not** get through. Bypass it — the
cross-platform script it delegates to takes no input:
```
python build_files/utils/make_update.py --no-blender --architecture x86_64
```

---

## 4. Verified API facts for the patch (Cycles 5.2)

`grep -rn "BL::" intern/cycles/` → **zero matches**. Confirmed: the RNA wrapper layer is gone.

### Reading the point cloud (`pointcloud.cpp:124-125, 252-253`)
```cpp
const blender::PointCloud *b_pointcloud = blender::id_cast<blender::PointCloud *>(b_ob_info.object_data);
const blender::Span<blender::float3> b_positions = b_pointcloud.positions();
const blender::bke::AttributeAccessor b_attributes = b_pointcloud.attributes();
```
Named typed lookup on the POINT domain, then deref to a span (`pointcloud.cpp:213-214`):
```cpp
const blender::VArraySpan b_radius = *b_attributes.lookup<float>("radius", blender::bke::AttrDomain::Point);
if (b_radius.is_empty()) { /* attribute absent */ }
```
Quaternion works the same via `lookup<blender::math::Quaternion>(...)`. Converter at
`attribute_convert.h:111-120` — note **w-first** (`make_float4(w, x, y, z)`) and
`layout_compatible = false`, so the quat data **must be copied**, not shared.

### Prototype geometry — sync once, share (`geometry.cpp:98-101`)
```cpp
Geometry *BlenderSync::sync_geometry(BObjectInfo &b_ob_info, bool object_updated,
                                     bool use_particle_hair, TaskPool *task_pool)
```
`GeometryKey` (`id_map.h:262-281`) is just **(object-data ID ptr, geom type)**. Call once for the
prototype with `task_pool = nullptr` (synchronous), reuse the returned `Geometry*` for all N.
`geometry_synced` (`geometry.cpp:115-120, 186`) makes repeat calls early-out, so per-instance calls
are safe but wasteful.

⚠️ If the prototype object has modifiers, `BKE_object_is_modified` makes the key the *object* ID
instead of the object-data ID (`geometry.cpp:104-109`) — still fine for a single prototype.

### ObjectKey (`id_map.h:212-255`)
```cpp
enum { OBJECT_PERSISTENT_ID_SIZE = 8 /* MAX_DUPLI_RECUR in Blender. */ };
struct ObjectKey { void *parent; int id[8]; void *ob; bool use_particle_hair; ... };
```
- Fixed **8 ints**, all significant (`memcmp` over 32 bytes).
- The ctor does `memcpy(id, id_, sizeof(id))` = **32 bytes unconditionally**. Passing a pointer to a
  shorter array reads out of bounds. Synthesize a full `int id[8]` on the stack.
- Synthetic keys are viable: `id[0] = point_index`, `parent = <pointcloud Object*>`,
  `ob = <prototype real_object>`. Key space effectively unbounded (2^31 × 8 slots).
- **Collision hazard:** real dupli paths also start at `id[0] = <dupli index>`. Use a magic tag in
  `id[7]` (or a parent pointer no real dupli path uses) to stay disjoint.

### Object creation (`scene.cpp:942-950`, `object.cpp:244-286`)
Never `new` an Object and never call `create_node<Object>()` directly — go through
`object_map.add_or_update(&object, &b_ob.id, &b_parent->id, key)`, which creates **and** registers
**and** marks-used in one step. `set_tfm` is hand-written (`scene/object.cpp:476-487`) and mutates
the transform via `adjust_volume_tfm` — do not write the socket directly.

### 🔴 RISK — `object_map` is an ordered `std::map` and is the likely new bottleneck

`id_map` holds a plain `std::map<ObjectKey, Object*>` (`util/map.h:12`, `id_map.h:199-204`) with
**no internal locking**. Consequences:

1. **`post_sync()` garbage-collects anything not marked used on every pass** (`id_map.h:143`), so we
   *cannot* skip registering our N instances — they'd be deleted.
2. That means **N red-black-tree insert/lookups per sync**, O(log N) each with poor cache locality,
   over a 32-byte key. At 1M–10M instances this could plausibly cost more than the ~86 ms
   `object_duplilist` expansion we're trying to eliminate — i.e. **it could eat the entire win.**
3. `add_or_update` is **not thread-safe**, so this cost cannot simply be threaded away.

**This is the primary technical risk to Tier 1 and must be measured at M0, not assumed.** If it
dominates, the fix is a dedicated storage path for instancer-owned objects (a flat
`vector<Object*>` indexed by point index, owned by the instancer and excluded from `object_map`'s
GC) rather than forcing every synthetic instance through the generic map. Design M0 so this is
measurable separately from the rest of the sync.

### Per-instance parity gaps (we have no `DupliObject`)
`object.cpp:339-351` derives `dupli_generated`, `dupli_uv` and `random_id` from
`dupli_object_current`. We must synthesize all three — `hash_uint2(point_index, seed)` from
`util/hash.h` is the natural `random_id` analogue.

`sync_object_attributes` (`object.cpp:400-459`) is a **hard divergence point**: instancer-source
Attribute shader nodes go through `BKE_object_dupli_find_rgba_attribute`, which requires a real
`DupliObject*`. With synthetic instances those lookups silently fall back to the object-level value.
To support them we must populate `object->attributes` ourselves with
`ParamValue(name, TypeFloat4, 1, &value)`. Defer past M0, but do not forget it.

### Authoring side (validated against stock 5.2 before the patch existed)

- `bpy.data.pointclouds.new(name)` then **`pc.resize(count)`** — `pc.points` is a read-only
  collection with no `.add()`. A `position` FLOAT_VECTOR attribute on the POINT domain appears
  automatically; only `pscale` needs `attributes.new(...)`.
- Custom properties survive save/reload with the types the C++ needs:
  `ob["cycles_render_instancer"] = True` → Python `bool` → **`IDP_BOOLEAN`** (the marker check must
  accept `IDP_BOOLEAN` *and* `IDP_INT`, not just int);
  `ob["cycles_instance_object"] = proto` → `bpy.types.Object` → **`IDP_ID`**, read via
  `IDP_ID_get(prop)`. Note `IDP_Id` (lowercase d) does **not** exist in 5.2.
- Scene builder: `scratchpad/build_instancer_scene.py` — emits the GN baseline and the marked
  point cloud side by side at identical N, so the same .blend is a valid A/B across stock and
  patched binaries (stock simply ignores the markers).

### Threading precedent
There is **no bulk parallel object creation** in `intern/cycles/` — the `TaskPool` is geometry-only,
and instances explicitly opt out of it (`object.cpp:239-241`). The shape to copy for a parallel fill
is `ObjectManager::device_update_transforms` (`scene/object.h:173, 199-202`): allocate serially,
then parallelize socket writes over index ranges.
