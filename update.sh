#!/usr/bin/env sh
# autosentry updater
#
# Re-runs the canonical installer (install.sh from this repo), which is
# idempotent and behaves as an upgrade-in-place for whichever backend
# autosentry was installed with (uv tool / pipx / pip --user).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/update.sh | sh
#
# Honors the same environment variables as install.sh:
#   AUTOSENTRY_VERSION   Pin to a specific version. Default: latest.
#   AUTOSENTRY_PRE       If "1", allow pre-release versions.
#   AUTOSENTRY_METHOD    Force uv | pipx | pip. Default: auto-detect.
#   AUTOSENTRY_PYTHON    Python interpreter for pipx/pip backends.
#   AUTOSENTRY_QUIET     If "1", suppress non-error output.
#
# Prefer the CLI when it's installed:
#   autosentry update           # equivalent to this script
#   autosentry update --check   # report current vs latest, no install
#   autosentry update --pre     # allow pre-releases
set -eu

INSTALL_URL="${AUTOSENTRY_INSTALL_URL:-https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh}"

if command -v autosentry >/dev/null 2>&1; then
    # If the CLI is on PATH and not broken, route through `autosentry update`
    # so users get the version-detection and --check niceties.
    if autosentry --version >/dev/null 2>&1; then
        exec autosentry update "$@"
    fi
fi

# Fallback: download install.sh and execute it. install.sh is idempotent.
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "${INSTALL_URL}" | sh
elif command -v wget >/dev/null 2>&1; then
    wget -qO- "${INSTALL_URL}" | sh
else
    echo "error: need curl or wget to fetch ${INSTALL_URL}" >&2
    exit 1
fi
