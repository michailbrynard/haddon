#!/usr/bin/env bash
# The site build as Cloudflare Workers Builds runs it, from site/ after its own `npm ci`. The API reference is
# made from the OpenAPI document the code produces, so the package is installed into a .venv first, the same
# one `make site` uses. The build image's python3 must be 3.12 or later; set PYTHON_VERSION if it is not.
set -euo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
python3 -m venv "$repo/.venv"
"$repo/.venv/bin/pip" install --quiet --upgrade pip
"$repo/.venv/bin/pip" install --quiet -e "$repo"

export ASTRO_TELEMETRY_DISABLED=1
export HADDON_PYTHON="$repo/.venv/bin/python"
export DOCS_SITE_URL="${DOCS_SITE_URL:-https://haddon.dev}"
cd "$repo/site"
npm run build
