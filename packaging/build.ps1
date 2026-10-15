# Reproducible build of the TF3 Planner desktop app (PyInstaller one-folder) -> dist\TF3Planner\
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 [-Python C:\Path\python.exe] [-Test] [-Zip]
#
#   -Python  interpreter for the build venv (default: py -3.13, then python on PATH); Python 3.13 x64
#   -Test    run packaging\smoke_test.py against the result (starts the exe, checks /control.html, stops it)
#   -Zip     also pack dist\TF3Planner-<version>-win64.zip (portable download)
#
# The build venv (.venv-build) is separate from the dev venv (.venv) and is created from
# packaging\requirements.txt, so the result does not depend on whatever is installed for development.
param(
    [string]$Python = "",
    [switch]$Test,
    [switch]$Zip,
    [string]$PyInstallerVersion = "6.22.3"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Venv = Join-Path $Root ".venv-build"
$VPy = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $VPy)) {
    Write-Host "== creating build venv $Venv"
    if ($Python) { & $Python -m venv $Venv }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { & py -3.13 -m venv $Venv }
    else { & python -m venv $Venv }
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
Write-Host "== installing requirements"
& $VPy -m pip install --disable-pip-version-check -q -r packaging\requirements.txt "pyinstaller==$PyInstallerVersion"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "== PyInstaller"
& $VPy -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\tf3planner.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$Out = Join-Path $Root "dist\TF3Planner"
$bytes = (Get-ChildItem $Out -Recurse -File | Measure-Object -Property Length -Sum).Sum
$files = (Get-ChildItem $Out -Recurse -File).Count
Write-Host ("== built {0}: {1:N0} MB, {2} files" -f $Out, ($bytes / 1MB), $files)

$Version = (& $VPy -c "import sys; sys.path.insert(0, '.'); from tf3planner import config; print(config.APP_VERSION)").Trim()
if ($Zip) {
    $ZipPath = Join-Path $Root "dist\TF3Planner-$Version-win64.zip"
    if (Test-Path $ZipPath) { Remove-Item $ZipPath }
    Compress-Archive -Path $Out -DestinationPath $ZipPath -CompressionLevel Optimal
    Write-Host ("== zip {0}: {1:N0} MB" -f $ZipPath, ((Get-Item $ZipPath).Length / 1MB))
}
if ($Test) {
    & $VPy -X utf8 packaging\smoke_test.py $Out
    if ($LASTEXITCODE -ne 0) { throw "smoke test failed" }
}
