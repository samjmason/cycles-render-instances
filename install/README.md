# Build scripts

One-shot scripts that clone Blender 5.2, apply the patch, bundle the addon, and
build a portable Blender you can run in place — optionally with GPU kernels
compiled against your own CUDA/OptiX.

Run them **from this `install/` folder** (they locate the patch and addon
relative to themselves).

## Windows (validated)

Needs: Git + Git LFS, Python 3, and Visual Studio 2022 (Community is fine) with
the "Desktop development with C++" workload. The script checks that your MSVC is
new enough (Blender 5.2 requires 17.14.14 / compiler 19.44.35216 or newer) and
stops with a clear message if not.

```powershell
# CPU-only (works on any machine):
.\build-windows.ps1

# GPU: also build CUDA + OptiX kernels, auto-matched to your card:
.\build-windows.ps1 -Gpu gpu

# custom location and a specific/multi arch:
.\build-windows.ps1 -SourceDir D:\blender-build -Gpu gpu -CudaArch sm_89
```

The CUDA Toolkit must be installed for `-Gpu gpu`
(`winget install Nvidia.CUDA`). OptiX headers are fetched automatically.

## Linux / macOS (NOT tested by the author)

Written from Blender's official build docs, but not run. The Windows script is
the validated one. Please report what breaks.

```bash
# CPU-only:
./build-posix.sh

# Linux GPU (needs CUDA Toolkit; macOS has no CUDA/OptiX):
./build-posix.sh --gpu
```

## Notes

- First build is ~30-45 minutes; GPU kernels add more. Re-running is
  incremental and skips the clone, library fetch, and patch if already done.
- Needs ~100 GB of free disk.
- CUDA kernels are built for one GPU architecture by default (auto-detected).
  For a mixed render farm, pass several, e.g. `-CudaArch sm_75;sm_86;sm_89`
  (Windows) — this is slower to compile but runs on more cards. OptiX kernels
  are portable across RTX cards regardless.
- The result is a self-contained folder. Copy it to another machine of the same
  OS to run without rebuilding (subject to GPU-arch coverage above).
