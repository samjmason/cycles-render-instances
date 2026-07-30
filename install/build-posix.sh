#!/usr/bin/env bash
# Build a patched Blender 5.2 with the Cycles render-instances feature.
#
# ---------------------------------------------------------------------------
# STATUS: NOT TESTED BY THE AUTHOR. The Windows script (build-windows.ps1) is
# the validated one. This script follows Blender's official build process
# (https://developer.blender.org/docs/handbook/building_blender/) plus this
# patch, but it has not been run on Linux or macOS. Treat it as a starting
# point, and please report what breaks.
# ---------------------------------------------------------------------------
#
# Usage:
#   ./build-posix.sh [--dir <path>] [--gpu] [--cuda-arch <sm_XX|auto>]
#
#   --dir        Where to clone and build (default: ./blender-build, needs ~100 GB)
#   --gpu        Also build CUDA + OptiX kernels (Linux only; needs CUDA Toolkit)
#   --cuda-arch  GPU arch for -gpu. 'auto' detects via nvidia-smi. Default: auto
#
# Prerequisites (install via your package manager first; see Blender's docs):
#   git, git-lfs, python3, cmake, and a C/C++ toolchain
#   (build-essential on Debian/Ubuntu, Xcode command line tools on macOS).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
PATCH="$REPO_ROOT/patches/0001-cycles-render-instances.patch"
ADDON="$REPO_ROOT/addon/cycles_render_instances.py"
BRANCH="blender-v5.2-release"

DIR="./blender-build"
GPU=0
CUDA_ARCH="auto"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --gpu) GPU=1; shift ;;
    --cuda-arch) CUDA_ARCH="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

info() { printf '\n==> %s\n' "$1"; }
ok()   { printf '    %s\n' "$1"; }
die()  { printf 'ERROR: %s\n' "$1" >&2; exit 1; }

# --------------------------------------------------------------- prerequisites
info "Checking prerequisites"
for tool in git python3 cmake; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not found. Install it and retry."
done
git lfs version >/dev/null 2>&1 || die "git-lfs not installed."
[ -f "$PATCH" ] || die "Patch not found at $PATCH. Run from the repo's install/ folder."
[ -f "$ADDON" ] || die "Addon not found at $ADDON."
ok "Prerequisites present"

case "$(uname -s)" in
  Darwin) ARCH_FLAG="$([ "$(uname -m)" = "arm64" ] && echo arm64 || echo x86_64)"; PLATFORM="macos" ;;
  Linux)  ARCH_FLAG="x86_64"; PLATFORM="linux" ;;
  *) die "Unsupported platform $(uname -s)." ;;
esac
if [ "$PLATFORM" = "macos" ] && [ "$GPU" = "1" ]; then
  die "GPU (CUDA/OptiX) is not available on macOS. Drop --gpu."
fi

# ---------------------------------------------------------------------- clone
DIR="$(mkdir -p "$DIR" && cd "$DIR" && pwd)"
BLENDER_DIR="$DIR/blender"
if [ -d "$BLENDER_DIR/.git" ]; then
  ok "Blender source already present, skipping clone"
else
  info "Cloning Blender $BRANCH"
  git clone --branch "$BRANCH" --single-branch \
    https://projects.blender.org/blender/blender.git "$BLENDER_DIR"
fi

# ----------------------------------------------------------- precompiled libs
info "Fetching precompiled libraries"
python3 "$BLENDER_DIR/build_files/utils/make_update.py" --no-blender --architecture "$ARCH_FLAG"
ok "Libraries fetched"

# ---------------------------------------------------------------- apply patch
info "Applying the render-instances patch"
cd "$BLENDER_DIR"
if git apply --reverse --check "$PATCH" 2>/dev/null; then
  ok "Patch already applied, skipping"
elif git apply --check "$PATCH" 2>/dev/null; then
  git apply "$PATCH"
  ok "Patch applied (4 files)"
else
  die "Patch does not apply cleanly. Wrong Blender version?"
fi
cd - >/dev/null

# ------------------------------------------------------------------ GPU setup
CMAKE_GPU_ARGS=()
if [ "$GPU" = "1" ]; then
  info "Configuring GPU (CUDA + OptiX)"
  command -v nvcc >/dev/null 2>&1 || die "CUDA Toolkit (nvcc) not found. Install it or drop --gpu."
  CUDA_DIR="$(dirname "$(dirname "$(command -v nvcc)")")"
  ok "CUDA Toolkit: $CUDA_DIR"

  OPTIX_DIR="$DIR/optix-sdk"
  if [ ! -f "$OPTIX_DIR/include/optix.h" ]; then
    info "Fetching OptiX headers"
    git clone --depth 1 https://github.com/NVIDIA/optix-dev.git "$OPTIX_DIR"
  fi
  ok "OptiX headers: $OPTIX_DIR"

  if [ "$CUDA_ARCH" = "auto" ]; then
    CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')"
    if printf '%s' "$CC" | grep -qE '^[0-9]+\.[0-9]+$'; then
      CUDA_ARCH="sm_$(printf '%s' "$CC" | tr -d '.')"
      ok "Detected compute capability $CC -> $CUDA_ARCH"
    else
      ok "Could not detect GPU arch; building all supported archs (slower)"
      CUDA_ARCH=""
    fi
  fi

  CMAKE_GPU_ARGS=(
    "-DWITH_CYCLES_CUDA_BINARIES=ON"
    "-DWITH_CYCLES_DEVICE_OPTIX=ON"
    "-DOPTIX_ROOT_DIR=$OPTIX_DIR"
    "-DCUDA_TOOLKIT_ROOT_DIR=$CUDA_DIR"
  )
  [ -n "$CUDA_ARCH" ] && CMAKE_GPU_ARGS+=("-DCYCLES_CUDA_BINARIES_ARCH=$CUDA_ARCH")
fi

# --------------------------------------------------------------------- build
info "Building Blender (first build is ~30-45 min; GPU adds more)"
BUILD_DIR="$DIR/build_${PLATFORM}_release"
cmake -S "$BLENDER_DIR" -B "$BUILD_DIR" \
  -C "$BLENDER_DIR/build_files/cmake/config/blender_release.cmake" \
  -DCMAKE_BUILD_TYPE=Release \
  "${CMAKE_GPU_ARGS[@]}"
cmake --build "$BUILD_DIR" --target install -j "$(getconf _NPROCESSORS_ONLN)"

EXE="$(find "$BUILD_DIR/bin" -name blender -type f 2>/dev/null | head -1)"
[ -n "$EXE" ] || die "Build finished but the blender binary was not found."
ok "Built: $EXE"

# ---------------------------------------------------------------- bundle addon
info "Bundling the front-end addon"
VER_DIR="$(find "$(dirname "$EXE")" -maxdepth 1 -type d -regex '.*/[0-9]+\.[0-9]+' | head -1)"
if [ -n "$VER_DIR" ]; then
  mkdir -p "$VER_DIR/scripts/addons"
  cp "$ADDON" "$VER_DIR/scripts/addons/"
  ok "Addon copied. Enable it via Edit > Preferences > Add-ons (search 'Render Instances')."
else
  ok "Could not find version folder; install the addon manually from $ADDON"
fi

printf '\n=======================================================\n'
printf ' Done. Portable Blender:\n   %s\n\n' "$EXE"
printf ' Run it, enable the addon, select a geometry-nodes scatter,\n'
printf " tick 'Render Instances' in Object Properties, and render.\n"
printf '=======================================================\n'
