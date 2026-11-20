#!/usr/bin/env bash
# Start the Microservice-Scripts setup app: builds the page when needed, then
# runs the local server and opens it in your browser. Extra arguments go to
# the server, e.g. --port 5300.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)/ui"

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: $1 not found: $2" >&2; exit 1; }
}
need dotnet "install the .NET 10 SDK (https://dotnet.microsoft.com/download)"
need npm "install Node.js 20 or newer (https://nodejs.org)"

cd "$here/frontend"
[ -d node_modules ] || npm ci --no-fund --no-audit
page="$here/backend/wwwroot/index.html"
if [ ! -f "$page" ] || [ -n "$(find src index.html package.json vite.config.ts -newer "$page" 2>/dev/null | head -1)" ]; then
  npm run build
fi

export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
exec dotnet run --project "$here/backend" -c Release -- --open "$@"
