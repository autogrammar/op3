#!/usr/bin/env bash
set -euo pipefail
_KORU_RT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PROJECT="$(cd "${_KORU_RT}/../.." && pwd)"
cd "${_PROJECT}"
# Keep the generated launcher aligned with the lane selected during init.
# Without this source, ambient IDE variables can silently replace the persisted
# lane (for example a JetBrains terminal can override a Qoder/Cursor project).
# shellcheck source=/dev/null
source "${_KORU_RT}/shell-env.sh"
exec koru autonomous up --project "${_PROJECT}" --agent-lane auto "$@"
