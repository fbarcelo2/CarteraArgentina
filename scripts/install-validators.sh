#!/usr/bin/env bash
# Install the external validators that are not Python packages: single-binary
# releases for shell, YAML, TOML, Dockerfile, spelling, Actions and secrets.
#
# Asset names carry version numbers and platform triples, so each one is matched
# by pattern against the project's latest release instead of being reconstructed
# from a template — the reconstruction is what breaks silently.
#
# Usage: scripts/install-validators.sh [--version-check]
#   DEST=/custom/dir   install somewhere other than ~/.local/bin
set -euo pipefail

DEST="${DEST:-$HOME/.local/bin}"
mkdir -p "$DEST"
ARCH="$(uname -m)"
[[ "$ARCH" == "aarch64" ]] && ARCH_ALT="arm64" || ARCH_ALT="x86_64"

# GitHub's API answers sixty requests an hour without credentials, which is easy
# to exhaust when this runs from CI and from a workstation at once. Use a token
# when one is around: GITHUB_TOKEN in Actions, GH_TOKEN locally.
api_get() {
  local token="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
  if [[ -n "$token" ]]; then
    curl -sSL -H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json" "$1"
  else
    curl -sSL -H "Accept: application/vnd.github+json" "$1"
  fi
}

release_json() {
  local payload
  payload="$(api_get "https://api.github.com/repos/$1/releases/latest")"
  # An error payload carries no "assets" key. Crashing on the missing key is what
  # turned a rate limit into a confusing CI failure, so say what happened instead.
  if ! grep -q '"assets"' <<<"$payload"; then
    echo "FAIL  no release from the GitHub API for $1: $(grep -o '"message": *"[^"]*"' <<<"$payload" | head -1)" >&2
    return 1
  fi
  printf '%s' "$payload"
}

latest_tag() {
  release_json "$1" | grep -oP '"tag_name":\s*"\K[^"]+'
}

asset_url() {
  # repo, asset regex
  local payload
  # Return before Python runs: otherwise a rate limit prints a traceback on top of
  # the message that actually explains what happened.
  payload="$(release_json "$1")" || return 1
  python3 -c "
import json, re, sys
pattern = sys.argv[1]
release = json.load(sys.stdin)
for asset in release['assets']:
    if re.fullmatch(pattern, asset['name']):
        print(asset['browser_download_url'])
        break
else:
    sys.exit(f'no asset matching {pattern!r} in {release[\"tag_name\"]}')
" "$2" <<<"$payload"
}

install_binary() {
  # name, repo, asset regex, member name inside the archive
  local name="$1" repo="$2" pattern="$3" member="$4"
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  local url
  url="$(asset_url "$repo" "$pattern")"
  curl -sSL "$url" -o "$tmp/download"

  case "$url" in
    *.tar.gz) tar -xzf "$tmp/download" -C "$tmp" ;;
    *.tar.xz) tar -xJf "$tmp/download" -C "$tmp" ;;
    *.zip) unzip -q "$tmp/download" -d "$tmp" ;;
    *.gz) gzip -dc "$tmp/download" > "$tmp/$member" ;;
    *) mv "$tmp/download" "$tmp/$member" ;;
  esac

  local found
  found="$(find "$tmp" -type f -name "$member" | head -1)"
  [[ -n "$found" ]] || { echo "FAIL  $name: no member named $member in $url" >&2; return 1; }
  install -m 0755 "$found" "$DEST/$name"
  printf '%-12s %-10s %s\n' "$name" "$(latest_tag "$repo")" "$("$DEST/$name" --version 2>&1 | head -1)"
}

install_binary gitleaks   gitleaks/gitleaks   'gitleaks_.*_linux_x64\.tar\.gz'                  gitleaks
install_binary shellcheck koalaman/shellcheck "shellcheck-v.*\.linux\.${ARCH_ALT}\.tar\.xz"      shellcheck
install_binary actionlint rhysd/actionlint    "actionlint_.*_linux_(amd64|${ARCH_ALT})\.tar\.gz" actionlint
install_binary hadolint   hadolint/hadolint   'hadolint-linux-x86_64$'                           hadolint
install_binary taplo      tamasfe/taplo       "taplo-linux-${ARCH_ALT}\.gz$"                     taplo
install_binary typos      crate-ci/typos      "typos-v.*-${ARCH_ALT}-unknown-linux-musl\.tar\.gz" typos
install_binary sg         ast-grep/ast-grep   "app-${ARCH_ALT}-unknown-linux-gnu\.zip"           ast-grep

echo
echo "installed into $DEST — make sure it is on your PATH"
