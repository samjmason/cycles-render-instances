<#
.SYNOPSIS
    Build a patched Blender 5.2 with the Cycles render-instances feature.

.DESCRIPTION
    Clones Blender 5.2, fetches its precompiled libraries, applies the patch,
    bundles the front-end addon, and builds a portable Blender you can run in
    place. Optionally compiles GPU (CUDA + OptiX) kernels against your own
    toolkit.

    Every step here was validated on Windows 11 + VS 2022 + RTX 4090. It is not
    a Blender-official script; it just automates the documented build process
    plus this patch.

.PARAMETER SourceDir
    Where to clone and build. Needs ~100 GB free. Default: .\blender-build

.PARAMETER Gpu
    'cpu'   - CPU only, fastest to build, works on any machine (default).
    'gpu'   - Also build CUDA + OptiX kernels. Needs the CUDA Toolkit installed
              and adds significant build time.

.PARAMETER CudaArch
    Only used with -Gpu gpu. 'auto' detects your card's compute capability via
    nvidia-smi and builds just that (fastest). Or pass e.g. 'sm_89' or
    'sm_75;sm_86;sm_89' for a multi-card farm. Default: auto

.EXAMPLE
    .\build-windows.ps1
    CPU-only build in .\blender-build

.EXAMPLE
    .\build-windows.ps1 -SourceDir D:\blender-build -Gpu gpu
    GPU build, kernels auto-matched to your card
#>

[CmdletBinding()]
param(
    [string]$SourceDir = ".\blender-build",
    [ValidateSet('cpu', 'gpu')][string]$Gpu = 'cpu',
    [string]$CudaArch = 'auto'
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$Patch = Join-Path $RepoRoot "patches\0001-cycles-render-instances.patch"
$Addon = Join-Path $RepoRoot "addon\cycles_render_instances.py"
$Branch = "blender-v5.2-release"

function Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Die($m)  { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- prerequisites
Info "Checking prerequisites"

foreach ($tool in 'git', 'python') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Die "$tool not found on PATH. Install it and retry."
    }
}
if (-not (git lfs version 2>$null)) { Die "git-lfs not installed. Install Git LFS and retry." }

# Patch and addon must be present (script is meant to run from inside the repo).
if (-not (Test-Path $Patch)) { Die "Patch not found at $Patch. Run this from the repo's install\ folder." }
if (-not (Test-Path $Addon)) { Die "Addon not found at $Addon." }

# Locate the MSVC compiler and enforce Blender 5.2's minimum. This is the single
# most common failure: Blender requires MSVC 19.44.35216 (VS 2022 17.14.14) or
# newer, and an older 17.14.x is rejected at configure time.
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$clExe = $null
if (Test-Path $vswhere) {
    $vsPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
    if ($vsPath) {
        $ver = Get-Content (Join-Path $vsPath "VC\Auxiliary\Build\Microsoft.VCToolsVersion.default.txt") -ErrorAction SilentlyContinue
        if ($ver) { $clExe = Join-Path $vsPath "VC\Tools\MSVC\$($ver.Trim())\bin\Hostx64\x64\cl.exe" }
    }
}
if (-not $clExe -or -not (Test-Path $clExe)) {
    Die "Visual Studio 2022 with C++ tools not found. Install VS 2022 (Community is fine) with the 'Desktop development with C++' workload."
}
# cl.exe prints its version banner to stderr. Do the stderr merge in cmd, not
# in PowerShell: with ErrorActionPreference=Stop, a PowerShell '2>&1' on a native
# command turns that banner into a terminating NativeCommandError.
$clOut = (cmd /c "`"$clExe`" 2>&1") | Select-Object -First 1
if ($clOut -match 'Version (\d+)\.(\d+)\.(\d+)') {
    $clVer = [version]("$($Matches[1]).$($Matches[2]).$($Matches[3])")
    $minVer = [version]"19.44.35216"
    if ($clVer -lt $minVer) {
        Die "MSVC compiler $clVer is too old. Blender 5.2 needs >= $minVer (VS 2022 17.14.14). Update Visual Studio and retry."
    }
    Ok "MSVC $clVer OK"
}
else { Die "Could not determine MSVC version from cl.exe." }

# CMake: prefer PATH, else fall back to the one VS bundles.
$cmake = (Get-Command cmake -ErrorAction SilentlyContinue).Source
if (-not $cmake) {
    $bundled = Join-Path $vsPath "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin"
    if (Test-Path (Join-Path $bundled "cmake.exe")) {
        $cmake = Join-Path $bundled "cmake.exe"
        $env:PATH = "$env:PATH;$bundled"
        Ok "Using Visual Studio's bundled CMake"
    }
    else { Die "CMake not found on PATH and not bundled with VS. Install CMake." }
}
else { Ok "CMake found: $cmake" }

# ---------------------------------------------------------------------- clone
$SourceDir = [System.IO.Path]::GetFullPath($SourceDir)
$BlenderDir = Join-Path $SourceDir "blender"
New-Item -ItemType Directory -Force -Path $SourceDir | Out-Null

if (Test-Path (Join-Path $BlenderDir ".git")) {
    Ok "Blender source already present, skipping clone"
}
else {
    Info "Cloning Blender $Branch (this downloads a few GB)"
    git clone --branch $Branch --single-branch https://projects.blender.org/blender/blender.git $BlenderDir
    Ok "Cloned"
}

# ----------------------------------------------------------- precompiled libs
Info "Fetching precompiled libraries"
# Use make_update.py directly, NOT make.bat update: the latter prompts
# interactively for the library download and cannot be automated.
Push-Location $BlenderDir
python build_files/utils/make_update.py --no-blender --architecture x86_64
Pop-Location
$libCount = (Get-ChildItem (Join-Path $BlenderDir "lib\windows_x64") -ErrorAction SilentlyContinue).Count
if ($libCount -lt 1) { Die "Library fetch produced nothing in lib\windows_x64." }
Ok "Libraries present ($libCount packages)"

# ---------------------------------------------------------------- apply patch
Info "Applying the render-instances patch"
Push-Location $BlenderDir
# If already applied (re-run), git apply --reverse --check succeeds; skip then.
$alreadyApplied = $false
git apply --reverse --check $Patch 2>$null; if ($?) { $alreadyApplied = $true }
if ($alreadyApplied) {
    Ok "Patch already applied, skipping"
}
else {
    git apply --check $Patch 2>$null
    if (-not $?) { Pop-Location; Die "Patch does not apply cleanly to this tree. Wrong Blender version?" }
    git apply $Patch
    Ok "Patch applied (4 files)"
}
Pop-Location

# ------------------------------------------------------------------ GPU setup
if ($Gpu -eq 'gpu') {
    Info "Configuring GPU (CUDA + OptiX)"

    $nvcc = (Get-Command nvcc -ErrorAction SilentlyContinue).Source
    if (-not $nvcc) {
        $cudaRoot = Get-ChildItem "${env:ProgramFiles}\NVIDIA GPU Computing Toolkit\CUDA" -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | Select-Object -First 1
        if ($cudaRoot) { $nvcc = Join-Path $cudaRoot.FullName "bin\nvcc.exe" }
    }
    if (-not $nvcc -or -not (Test-Path $nvcc)) {
        Die "CUDA Toolkit not found. Install it (winget install Nvidia.CUDA) or build with -Gpu cpu."
    }
    $cudaDir = Split-Path -Parent (Split-Path -Parent $nvcc)
    Ok "CUDA Toolkit: $cudaDir"

    # OptiX headers: header-only, safe to fetch if the user doesn't have the SDK.
    $optixDir = Join-Path $SourceDir "optix-sdk"
    if (-not (Test-Path (Join-Path $optixDir "include\optix.h"))) {
        Info "Fetching OptiX headers"
        git clone --depth 1 https://github.com/NVIDIA/optix-dev.git $optixDir
    }
    Ok "OptiX headers: $optixDir"

    # Compute capability -> sm_XX. nvidia-smi reports e.g. "8.9" for a 4090.
    if ($CudaArch -eq 'auto') {
        $cc = (& nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>$null | Select-Object -First 1)
        if ($cc -match '(\d+)\.(\d+)') {
            $CudaArch = "sm_$($Matches[1])$($Matches[2])"
            Ok "Detected GPU compute capability $cc -> $CudaArch"
        }
        else {
            Ok "Could not detect GPU arch; building all supported archs (slower)"
            $CudaArch = ''
        }
    }

    $BuildDir = Join-Path $SourceDir "build_windows_Release_x64_vc17_Release"
    New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null
    $cmakeArgs = @(
        "-DWITH_CYCLES_CUDA_BINARIES=ON",
        "-DWITH_CYCLES_DEVICE_OPTIX=ON",
        "-DOPTIX_ROOT_DIR=$($optixDir -replace '\\','/')",
        "-DCUDA_TOOLKIT_ROOT_DIR=$($cudaDir -replace '\\','/')"
    )
    if ($CudaArch) { $cmakeArgs += "-DCYCLES_CUDA_BINARIES_ARCH=$CudaArch" }
    # Prime the cache so make.bat's build picks these up.
    Push-Location $BlenderDir
    & $cmake @cmakeArgs -S $BlenderDir -B $BuildDir | Out-Null
    Pop-Location
    Ok "GPU configured (arch: $(if($CudaArch){$CudaArch}else{'all'}))"
}

# --------------------------------------------------------------------- build
Info "Building Blender (first build is ~30-45 min; GPU adds more)"
Push-Location $BlenderDir
cmd /c "make.bat release"
$buildOk = $?
Pop-Location
if (-not $buildOk) { Die "Build failed. See the output above for the first error." }

# Find the built exe.
$exe = Get-ChildItem $SourceDir -Recurse -Filter "blender.exe" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "bin\\Release" } | Select-Object -First 1
if (-not $exe) { Die "Build reported success but blender.exe was not found." }
$ReleaseDir = $exe.Directory.FullName
Ok "Built: $($exe.FullName)"

# ---------------------------------------------------------------- bundle addon
Info "Bundling the front-end addon"
# Drop it where Blender auto-discovers user addons in this portable build.
$addonTarget = Get-ChildItem $ReleaseDir -Directory | Where-Object { $_.Name -match '^\d+\.\d+$' } |
    Select-Object -First 1
if ($addonTarget) {
    $dest = Join-Path $addonTarget.FullName "scripts\addons"
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item $Addon $dest -Force
    Ok "Addon copied to $dest"
    Write-Host "    Enable it once via Edit > Preferences > Add-ons (search 'Render Instances')." -ForegroundColor Green
}
else {
    Write-Host "    Could not locate the version folder; install the addon manually from $Addon" -ForegroundColor Yellow
}

# ------------------------------------------------------------------- summary
Write-Host ""
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host " Done. Portable Blender is in:" -ForegroundColor Cyan
Write-Host "   $ReleaseDir" -ForegroundColor White
Write-Host ""
Write-Host " Run it:  `"$($exe.FullName)`"" -ForegroundColor White
Write-Host ""
Write-Host " Then: enable the addon, select a geometry-nodes scatter," -ForegroundColor White
Write-Host " tick 'Render Instances' in Object Properties, and render" -ForegroundColor White
Write-Host " or open a Cycles viewport." -ForegroundColor White
Write-Host "=======================================================" -ForegroundColor Cyan
