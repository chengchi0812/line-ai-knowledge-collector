#!/bin/bash
set -euo pipefail

PROJECT_DIR="$HOME/local-ai-transcriber"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"
RAW_BASE="https://raw.githubusercontent.com/chengchi0812/line-ai-knowledge-collector/main/local-worker"
PLIST="$HOME/Library/LaunchAgents/com.chi.local-ai-recovery.plist"
LABEL="com.chi.local-ai-recovery"
LOG_DIR="$PROJECT_DIR/logs"

if [ ! -x "$PYTHON" ]; then
  echo "找不到 Python venv：$PYTHON"
  exit 1
fi

mkdir -p "$PROJECT_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"

curl -fsSL "$RAW_BASE/notion_recovery_worker.py" -o "$PROJECT_DIR/notion_recovery_worker.py"
curl -fsSL "$RAW_BASE/notion_recovery_supervisor.py" -o "$PROJECT_DIR/notion_recovery_supervisor.py"

"$PIP" install -q pypdf

"$PYTHON" -m py_compile "$PROJECT_DIR/notion_recovery_worker.py"
"$PYTHON" -m py_compile "$PROJECT_DIR/notion_recovery_supervisor.py"

echo "=== Recovery Queue Peek ==="
cd "$PROJECT_DIR"
"$PYTHON" notion_recovery_supervisor.py --peek || true

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$PROJECT_DIR/notion_recovery_supervisor.py</string>
        <string>--loop</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>$HOME</string>
        <key>PATH</key>
        <string>$VENV_DIR/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/recovery.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/recovery-error.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST"

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "=== LaunchAgent Status ==="
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state =|pid =|PATH =>" || true

echo
echo "Recovery Supervisor 安裝完成。"
echo "Log: $LOG_DIR/recovery.log"
echo "Error log: $LOG_DIR/recovery-error.log"
