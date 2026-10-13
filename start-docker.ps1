$ErrorActionPreference = 'Stop'
$dockerDesktop = 'D:\Apps\Docker\DockerDesktop\Docker Desktop.exe'
$runtimeDir = 'D:\DockerRuntime'
if (-not (Test-Path -LiteralPath $dockerDesktop)) {
    throw "Docker Desktop not found: $dockerDesktop"
}
if (-not (Test-Path -LiteralPath $runtimeDir)) {
    New-Item -ItemType Directory -Path $runtimeDir | Out-Null
}
# Only the launched Docker process inherits this override; Windows settings stay intact.
$env:LOCALAPPDATA = $runtimeDir
Start-Process -FilePath $dockerDesktop -WindowStyle Hidden
Write-Output 'Starting Docker Desktop with runtime directory D:\DockerRuntime.'
