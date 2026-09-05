# Builds the feasibility image and runs it with Render's free-tier caps
# (512 MB RAM, 0.1 vCPU, no swap) as of the April 2026 pricing revamp --
# https://render.com/docs/free -- verify those numbers are still current
# before trusting a PASS here as "it'll work on Render", they're a guess
# frozen at the time this script was written, not a live lookup.
#
# Run from anywhere; it locates the repo root itself. Requires Docker
# Desktop running.
#
#   powershell -File tools/orca_render_feasibility/run_feasibility.ps1
#   powershell -File tools/orca_render_feasibility/run_feasibility.ps1 -StlPath "C:\path\to\model.stl"
#
# -StlPath is optional: when given, that file is bind-mounted read-only into
# the container and run_feasibility_test.py's mesh_stl scenario exercises it
# through build_mesh_hybrid_print. Omitting it just skips that one scenario
# (small_circle and star_zones still run) -- the file itself is never
# copied into the image or committed anywhere.
#
# Exit code mirrors run_feasibility_test.py's inside the container: 0 means
# every scenario completed (or cleanly skipped) under these caps, non-zero
# means at least one did not (see that script's own output for why -- OOM
# vs. a missing-library crash vs. a real modeling-constraint ValueError all
# look different and it tries to tell them apart).

param(
    [string]$StlPath
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Dockerfile = Join-Path $PSScriptRoot "Dockerfile"
$ImageTag = "trident-orca-feasibility:latest"

Write-Host "Building $ImageTag from $Dockerfile (context: $RepoRoot)" -ForegroundColor Cyan
docker build -f $Dockerfile -t $ImageTag $RepoRoot
if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed -- fix the Dockerfile before this test can mean anything." -ForegroundColor Red
    exit $LASTEXITCODE
}

$mountArgs = @()
if ($StlPath) {
    $StlPath = (Resolve-Path $StlPath).Path
    Write-Host "Mounting $StlPath -> /data/test.stl (read-only)" -ForegroundColor Cyan
    $mountArgs += @("-v", "${StlPath}:/data/test.stl:ro", "-e", "TRIDENT_TEST_STL_PATH=/data/test.stl")
} else {
    Write-Host "No -StlPath given -- mesh_stl scenario will report SKIPPED" -ForegroundColor DarkYellow
}

Write-Host "`nRunning under Render free-tier caps: --memory=512m --cpus=0.1 --memory-swap=512m (no swap)" -ForegroundColor Cyan
$start = Get-Date
docker run --rm `
    --memory=512m `
    --memory-swap=512m `
    --cpus=0.1 `
    @mountArgs `
    $ImageTag
$exitCode = $LASTEXITCODE
$elapsed = (Get-Date) - $start

Write-Host "`nContainer run took $($elapsed.TotalSeconds.ToString('F1'))s, exit code $exitCode" -ForegroundColor Cyan
if ($exitCode -eq 137) {
    Write-Host "Exit 137 = the container's own process was SIGKILLed -- almost always" -ForegroundColor Yellow
    Write-Host "the 512 MB cgroup limit killing it directly (a harder OOM than the" -ForegroundColor Yellow
    Write-Host "in-process one run_feasibility_test.py tries to catch and report)." -ForegroundColor Yellow
}

exit $exitCode
