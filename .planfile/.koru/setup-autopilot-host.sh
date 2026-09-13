#!/usr/bin/env bash
set -euo pipefail
_RT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PROJECT="$(cd "${_RT}/../.." && pwd)"
cd "${_PROJECT}"
if command -v koru >/dev/null 2>&1; then
  exec koru autopilot setup-host "$@"
elif command -v python3 >/dev/null 2>&1; then
  exec python3 -m koru.cli autopilot setup-host "$@"
else
  echo "koru: not in PATH; try: python3 -m koru.cli autopilot setup-host" >&2
  exit 127
fi
