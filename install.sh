#!/usr/bin/env sh
# autosentry installer
#
# Installs (or upgrades) the `autosentry` CLI from PyPI using the best
# Python package manager available on the host. Prefers `uv` (fast, isolated
# tool environments), falls back to `pipx`, then plain `pip --user`.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/ulmentflam/autosentry/main/install.sh | sh
#
# Environment variables:
#   AUTOSENTRY_VERSION   Pin to a specific version, e.g. "0.2.0". Default: latest.
#   AUTOSENTRY_PRE       If "1", allow pre-release versions when resolving "latest".
#   AUTOSENTRY_METHOD    Force one of: uv | pipx | pip. Default: auto-detect.
#   AUTOSENTRY_PYTHON    Python interpreter for pipx/pip backends. Default: python3.
#   AUTOSENTRY_QUIET     If "1", suppress non-error output.
#
# Exit codes:
#   0   success
#   1   no usable Python toolchain
#   2   install command failed
#   3   post-install validation failed
#
# Idempotent. Safe to re-run; behaves as an upgrade-in-place.
set -eu

# --------------------------------------------------------------------------- #
# Tiny logging helpers — no colors when piped, no surprises in CI.
# --------------------------------------------------------------------------- #
if [ -t 1 ] && [ "${NO_COLOR:-}" = "" ]; then
    BOLD=$(printf '\033[1m')
    DIM=$(printf '\033[2m')
    RED=$(printf '\033[31m')
    GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m')
    RESET=$(printf '\033[0m')
else
    BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi

quiet() { [ "${AUTOSENTRY_QUIET:-0}" = "1" ]; }
say()   { quiet || printf "%s>>%s %s\n" "${BOLD}" "${RESET}" "$*"; }
warn()  { printf "%s!!%s %s\n" "${YELLOW}${BOLD}" "${RESET}" "$*" >&2; }
err()   { printf "%sxx%s %s\n" "${RED}${BOLD}" "${RESET}" "$*" >&2; }
ok()    { quiet || printf "%sok%s %s\n" "${GREEN}${BOLD}" "${RESET}" "$*"; }

# --------------------------------------------------------------------------- #
# Resolve target spec — handles pinned version and pre-release flag.
# --------------------------------------------------------------------------- #
target_spec() {
    if [ -n "${AUTOSENTRY_VERSION:-}" ]; then
        printf "autosentry==%s" "${AUTOSENTRY_VERSION}"
    else
        printf "autosentry"
    fi
}

pre_flag_for() {
    # method-specific flag for "allow pre-releases"
    if [ "${AUTOSENTRY_PRE:-0}" != "1" ]; then
        printf ""
        return
    fi
    case "$1" in
        uv|pipx) printf -- "--pre" ;;
        pip)     printf -- "--pre" ;;
    esac
}

# --------------------------------------------------------------------------- #
# Detect available install method.
# --------------------------------------------------------------------------- #
have() { command -v "$1" >/dev/null 2>&1; }

detect_method() {
    if [ -n "${AUTOSENTRY_METHOD:-}" ]; then
        case "${AUTOSENTRY_METHOD}" in
            uv|pipx|pip) printf "%s" "${AUTOSENTRY_METHOD}"; return ;;
            *) err "AUTOSENTRY_METHOD must be one of: uv, pipx, pip"; exit 1 ;;
        esac
    fi
    if have uv; then
        printf "uv"
    elif have pipx; then
        printf "pipx"
    elif have python3; then
        printf "pip"
    else
        err "no usable Python toolchain found (need one of: uv, pipx, python3)"
        err "install uv:    curl -LsSf https://astral.sh/uv/install.sh | sh"
        err "install pipx:  python3 -m pip install --user pipx"
        exit 1
    fi
}

# --------------------------------------------------------------------------- #
# Install via each backend.
# --------------------------------------------------------------------------- #
install_via_uv() {
    say "installing via uv tool install"
    # shellcheck disable=SC2046  # we want word-splitting on $(pre_flag_for ...)
    if uv tool list 2>/dev/null | grep -q '^autosentry '; then
        uv tool upgrade autosentry $(pre_flag_for uv) >&2
    else
        uv tool install $(pre_flag_for uv) "$(target_spec)" >&2
    fi
}

install_via_pipx() {
    say "installing via pipx"
    py="${AUTOSENTRY_PYTHON:-python3}"
    # shellcheck disable=SC2046
    if pipx list --short 2>/dev/null | awk '{print $1}' | grep -qx 'autosentry'; then
        pipx upgrade autosentry $(pre_flag_for pipx) >&2 || \
            pipx install --force --python "$py" $(pre_flag_for pipx) "$(target_spec)" >&2
    else
        pipx install --python "$py" $(pre_flag_for pipx) "$(target_spec)" >&2
    fi
}

install_via_pip() {
    say "installing via pip --user"
    py="${AUTOSENTRY_PYTHON:-python3}"
    # shellcheck disable=SC2046
    "$py" -m pip install --user --upgrade $(pre_flag_for pip) "$(target_spec)" >&2
    # Surface a PATH hint if --user bin dir isn't on PATH.
    user_base=$("$py" -c 'import site, sys; print(site.USER_BASE)' 2>/dev/null || true)
    if [ -n "${user_base}" ]; then
        user_bin="${user_base}/bin"
        case ":${PATH}:" in
            *":${user_bin}:"*) : ;;
            *) warn "${user_bin} is not on PATH — add it to use the autosentry command." ;;
        esac
    fi
}

# --------------------------------------------------------------------------- #
# Verify the install worked.
# --------------------------------------------------------------------------- #
post_check() {
    if ! have autosentry; then
        # uv tool installs land under $HOME/.local/bin (or similar); pipx too.
        warn "the 'autosentry' command isn't on PATH yet."
        warn "you may need to restart your shell or add the install bin dir to PATH."
        warn "uv:    eval \"\$(uv tool path)\" or ensure ~/.local/bin is on PATH"
        warn "pipx:  pipx ensurepath"
        # Don't fail — the package is installed, only PATH wiring is missing.
        return 0
    fi
    ver=$(autosentry --version 2>&1 || true)
    if [ -z "${ver}" ]; then
        err "autosentry was installed but '--version' returned nothing."
        return 3
    fi
    ok "${ver}"
}

# --------------------------------------------------------------------------- #
# Main.
# --------------------------------------------------------------------------- #
main() {
    say "autosentry installer"
    say "${DIM}target:${RESET} $(target_spec)"
    method=$(detect_method)
    say "${DIM}method:${RESET} ${method}"

    case "${method}" in
        uv)   install_via_uv   || { err "uv install failed";   exit 2; } ;;
        pipx) install_via_pipx || { err "pipx install failed"; exit 2; } ;;
        pip)  install_via_pip  || { err "pip install failed";  exit 2; } ;;
    esac

    post_check
    ok "next: 'autosentry init' in your project, then 'autosentry run'"
}

main "$@"
