#!/usr/bin/env bash
# One-shot setup + first run on macOS.   Usage:  cd ~/src/job-agent && ./scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data
LOG=data/setup.log
exec > >(tee "$LOG") 2>&1

step() { printf "\n\033[1;34m▶ %s\033[0m\n" "$*"; }

step "Python"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY=$c; break; fi
done
if [ -z "$PY" ]; then
  echo "Need Python 3.10+.  Install it with:  brew install python@3.12   then re-run this script."; exit 1
fi
echo "using $($PY --version) ($PY)"

step "Virtualenv + dependencies"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt pytest
.venv/bin/python -m playwright install chromium

step "Config"
if [ ! -d config ]; then
  cp -r config.example config && echo "created config/ from config.example/ — edit it with your details (git-ignored)"
else
  echo "config/ exists"
fi
mkdir -p resumes

step "Anthropic API key"
if [ -f .env ] && grep -q '^ANTHROPIC_API_KEY=sk-ant-' .env; then
  echo "already in .env"
else
  echo "Create one at https://platform.claude.com → Settings → API keys"
  read -r -s -p "Paste key (input hidden, Enter to skip for now): " KEY; echo
  if [ -n "${KEY:-}" ]; then
    printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" > .env && chmod 600 .env && echo "saved to .env"
  else
    echo "skipped — discovery works without it; scoring/applying needs it"
  fi
fi

step "Tests"
.venv/bin/python -m pytest -q tests

step "Doctor"
.venv/bin/python -m jobagent doctor

step "Verify companies (disables ones that don't respond)"
.venv/bin/python -m jobagent verify-companies --disable-failed | tee data/verify.txt

step "Discover jobs"
.venv/bin/python -m jobagent discover

if grep -q '^ANTHROPIC_API_KEY=sk-ant-' .env 2>/dev/null; then
  step "Score with Claude"
  .venv/bin/python -m jobagent score --limit 60
  step "Your queue"
  .venv/bin/python -m jobagent list -v --limit 15
fi

printf "\n\033[1;32m✔ Done.\033[0m  Log: %s\n" "$LOG"
echo "Next:  source .venv/bin/activate && python -m jobagent apply --next"
