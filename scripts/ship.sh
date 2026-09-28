#!/bin/zsh
# Ships a new feature of YouTube Zeus, GitHub first:
#   build + install, regenerate the agent guide, update the commit, push to GitHub,
#   and refresh the source snapshot kept in the Second Brain.
#
#   ./scripts/ship.sh "Short description of the feature"
#   ./scripts/ship.sh --no-build "message"      (code already built and installed)
#
# Write the CHANGELOG.md entry before shipping.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH=/opt/homebrew/bin:/usr/local/bin:$PATH

BUILD=1
if [[ "${1:-}" == "--no-build" ]]; then BUILD=0; shift; fi
MSG="${1:?usage: ship.sh [--no-build] \"message\"}"

WAS_RUNNING=0
pgrep -x "YouTube Zeus" >/dev/null && WAS_RUNNING=1
if (( BUILD )); then ./scripts/build.sh; fi

APP_BIN="/Applications/YouTube Zeus.app/Contents/MacOS/YouTube Zeus"
mkdir -p docs
"$APP_BIN" --cli guide --generic > docs/AGENT-GUIDE.md

git add -A
if ! git diff --cached --quiet; then
  git commit -q -m "$MSG"
  echo "Committed $(git rev-parse --short HEAD)"
fi
if git remote get-url origin >/dev/null 2>&1; then
  git push -q origin HEAD
  echo "Pushed to $(git remote get-url origin)"
fi

# Optional: a source snapshot kept in your vault. Set ZEUS_VAULT_COPY (or put it in scripts/ship.local,
# which is not committed), e.g. ZEUS_VAULT_COPY="$HOME/SecondBrain/youtube-zeus".
[[ -f scripts/ship.local ]] && source scripts/ship.local
VAULT_COPY="${ZEUS_VAULT_COPY:-}"
if [[ -n "$VAULT_COPY" && -d "$VAULT_COPY" ]]; then
  git archive HEAD | tar -x -C "$VAULT_COPY"
  git bundle create "$VAULT_COPY/youtube-zeus.gitbundle" --all 2>/dev/null
  HASH=$(git rev-parse --short HEAD)
  sed -i '' -E "s/at commit \`[0-9a-f]+\`/at commit \`$HASH\`/" "$VAULT_COPY/SNAPSHOT.md" 2>/dev/null || true
  echo "Vault snapshot refreshed ($HASH)"
fi

if (( BUILD && WAS_RUNNING )); then open -g -a "/Applications/YouTube Zeus.app"; fi
