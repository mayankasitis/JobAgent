#!/usr/bin/env bash
# Installs a macOS launchd job that runs `jobagent daily` every morning
# (discover + score + desktop notification). Default 09:07; override: ./install_schedule.sh 8 30
set -euo pipefail
HOUR="${1:-9}"; MINUTE="${2:-7}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LABEL="com.jobagent.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$ROOT/data" "$HOME/Library/LaunchAgents"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>jobagent</string><string>daily</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MINUTE</integer>
  </dict>
  <key>StandardOutPath</key><string>$ROOT/data/daily.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/daily.log</string>
</dict></plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed: runs daily at $(printf '%02d:%02d' "$HOUR" "$MINUTE"). Log: $ROOT/data/daily.log"
echo "Run now:    launchctl kickstart gui/$(id -u)/$LABEL"
echo "Uninstall:  launchctl bootout gui/$(id -u)/$LABEL && rm $PLIST"
