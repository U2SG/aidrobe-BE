param(
  [switch]$Restart,
  [switch]$Stop,
  [switch]$Build,
  [int]$ApiPort = 8000,
  [int]$MinioPort = 9000,
  [string]$HostIp = "100.89.226.13"
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$composeFile = Join-Path $root "docker-compose.yml"

if (-not (Test-Path $composeFile)) {
  Write-Error "docker-compose.yml not found: $composeFile"
  exit 1
}

function Invoke-Compose {
  param([string[]]$ComposeArgs)
  & docker compose -f $composeFile @ComposeArgs
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose failed with exit code $LASTEXITCODE"
  }
}

Set-Location $root

if ($Stop -or $Restart) {
  Write-Host "Stopping Aidrobe backend containers..."
  Invoke-Compose @("stop", "api", "worker", "scheduler", "nginx", "postgres", "redis", "minio")
}

if ($Stop) {
  Write-Host "Aidrobe backend stopped."
  exit 0
}

Write-Host "Starting Aidrobe backend containers at $([DateTime]::Now.ToString('s'))..."
Write-Host "Resetting one-shot setup containers..."
Invoke-Compose @("rm", "-f", "migrate", "minio-init")

$missingImage = $false
foreach ($imageName in @("aidrobe-api", "aidrobe-worker")) {
  & docker image inspect $imageName *> $null
  if ($LASTEXITCODE -ne 0) {
    $missingImage = $true
  }
}

$upArgs = @("up", "-d")
if ($Build -or $missingImage) {
  $upArgs += "--build"
} else {
  $upArgs += "--no-build"
}
$upArgs += @("api", "worker")
Invoke-Compose $upArgs

Write-Host "Aidrobe API : http://$HostIp`:$ApiPort/healthz"
Write-Host "Aidrobe S3  : http://$HostIp`:$MinioPort/aidrobe"
