#Requires -Version 7.0
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$projectRoot = (Get-Item -LiteralPath (Join-Path $PSScriptRoot '..')).FullName
$runtimePath = Join-Path $projectRoot '.runtime\python313'
$downloadPath = 'C:\HotWaterRuntime\python-3.13.17-embed-amd64.zip'
$downloadUrl = 'https://www.python.org/ftp/python/3.13.17/python-3.13.17-embed-amd64.zip'
$expectedHash = '9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38'
$python = Join-Path $runtimePath 'python.exe'
$readyPath = Join-Path $runtimePath 'hotwater-ready.txt'
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$requirements = Join-Path $projectRoot 'relay-src\relay\requirements.txt'

Write-Information "Preparing HotWater's Python 3.13 runtime at $runtimePath" -InformationAction Continue
if (Test-Path -LiteralPath $readyPath -PathType Leaf) {
    & $python -B -c 'import ssl, aiohttp, Crypto; print(ssl.OPENSSL_VERSION, aiohttp.__version__)'
    if ($LASTEXITCODE -ne 0) { throw 'The existing HotWater runtime failed its import check.' }
    return
}

$null = New-Item -ItemType Directory -Path 'C:\HotWaterRuntime' -Force
if (-not (Test-Path -LiteralPath $downloadPath -PathType Leaf)) {
    Invoke-WebRequest -Uri $downloadUrl -OutFile $downloadPath
}
if ((Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash -ne $expectedHash) {
    throw "Python archive checksum failed. Inspect $downloadPath."
}
Expand-Archive -LiteralPath $downloadPath -DestinationPath $runtimePath -Force
Set-Content -LiteralPath (Join-Path $runtimePath 'python313._pth') -Encoding ascii -Value @(
    'python313.zip', '.', 'site-packages', '..\..\relay-src\relay'
)
& $venvPython -B -m pip --disable-pip-version-check install --no-cache-dir --no-compile --only-binary=:all: `
    --require-hashes --upgrade --target (Join-Path $runtimePath 'site-packages') -r $requirements
if ($LASTEXITCODE -ne 0) { throw "Relay dependency installation failed with exit code $LASTEXITCODE." }
& $python -B -c 'import ssl, aiohttp, Crypto; print(ssl.OPENSSL_VERSION, aiohttp.__version__)'
if ($LASTEXITCODE -ne 0) { throw 'The new HotWater runtime failed its import check.' }
Set-Content -LiteralPath $readyPath -Value "Python 3.13.17 archive SHA256 $expectedHash"
Write-Information "HotWater runtime is ready at $runtimePath" -InformationAction Continue
