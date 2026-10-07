# Builds the two Lambda package folders in build/ (zipped later by Terraform).
#
# Pillow contains compiled code. A Windows "pip install Pillow" gives Windows binaries
# that crash on Lambda (Amazon Linux). So we download the Linux wheels explicitly
# for Python 3.12 / x86_64 with --platform and --only-binary.
#
# Usage (from the project root):  .\scripts\build_lambda.ps1
#
# Result:
#   build/job_api/    imgproc package only (boto3 is provided by the Lambda runtime)
#   build/processor/  imgproc package + Linux Pillow
# infra/lambda.tf zips these folders; run this script BEFORE terraform plan/apply.

# Stop at the first error instead of continuing with a half-built package.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $root "build"
$src = Join-Path $root "backend\src\imgproc"
# Prefer the project's virtual environment Python; fall back to the global one.
$python = Join-Path $root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# Start from a clean build folder every time.
if (Test-Path $build) { Remove-Item -Recurse -Force $build }
New-Item -ItemType Directory -Force (Join-Path $build "job_api"), (Join-Path $build "processor") | Out-Null

# INTERVIEW:
# Lambda runs on Amazon Linux, but this machine is Windows. "pip install --platform ...
# --only-binary=:all:" downloads pre-built Linux "wheel" files instead of the Windows
# ones, without needing Docker or a Linux machine. Only the processor needs Pillow.
Write-Host "Installing Linux Pillow wheels for Lambda (python3.12, x86_64)..."
& $python -m pip install --quiet `
    --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 `
    --implementation cp --python-version 3.12 --only-binary=:all: `
    --target (Join-Path $build "processor") `
    -r (Join-Path $root "backend\requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# Copy our own code into both packages (without local bytecode caches).
foreach ($dir in @("job_api", "processor")) {
    $dest = Join-Path $build "$dir\imgproc"
    Copy-Item -Recurse $src $dest
    Get-ChildItem -Path $dest -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
}

# Sanity check: the Pillow binary must be a Linux .so, not a Windows .pyd
$so = Get-ChildItem -Path (Join-Path $build "processor\PIL") -Filter "_imaging*.so" -ErrorAction SilentlyContinue
if (-not $so) { throw "Linux Pillow binary (_imaging*.so) not found - build is not Lambda-compatible" }
Write-Host "OK: found $($so[0].Name)"
Write-Host "Lambda packages ready in $build"
