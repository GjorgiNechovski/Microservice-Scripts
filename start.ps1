# Start the Microservice-Scripts setup app: builds the page when needed, then
# runs the local server and opens it in your browser. Extra arguments go to
# the server, e.g. -- --port 5300.
$ErrorActionPreference = 'Stop'
$here = Join-Path $PSScriptRoot 'ui'

foreach ($tool in @(
    @{ Name = 'dotnet'; Hint = 'install the .NET 10 SDK (https://dotnet.microsoft.com/download)' },
    @{ Name = 'npm'; Hint = 'install Node.js 20 or newer (https://nodejs.org)' })) {
  if (-not (Get-Command $tool.Name -ErrorAction SilentlyContinue)) {
    Write-Error "$($tool.Name) not found: $($tool.Hint)"
  }
}

Push-Location (Join-Path $here 'frontend')
try {
  if (-not (Test-Path 'node_modules')) { npm ci --no-fund --no-audit; if ($LASTEXITCODE) { exit $LASTEXITCODE } }
  $page = Join-Path $here 'backend/wwwroot/index.html'
  $sources = @('src', 'index.html', 'package.json', 'vite.config.ts') |
    ForEach-Object { Get-ChildItem $_ -Recurse -File } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not (Test-Path $page) -or $sources.LastWriteTime -gt (Get-Item $page).LastWriteTime) {
    npm run build; if ($LASTEXITCODE) { exit $LASTEXITCODE }
  }
}
finally { Pop-Location }

$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_NOLOGO = '1'
dotnet run --project (Join-Path $here 'backend') -c Release -- --open @args
