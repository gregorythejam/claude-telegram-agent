#!/bin/bash
# Запуск Telegram-агента (служба tg-agent: systemctl --user status tg-agent).
# Продолжает прошлый диалог; после команды /new начинает новый.
WORK="$HOME/telegram-agent"
cd "$WORK" || exit 1
PROJ="$HOME/.claude/projects/$(printf '%s' "$WORK" | sed 's/[^A-Za-z0-9]/-/g')"
CONT=""
ls "$PROJ"/*.jsonl >/dev/null 2>&1 && CONT="--continue"
[ -f "$TELEGRAM_STATE_DIR/new-session.flag" ] && CONT=""
exec "$HOME/.local/bin/claude" $CONT --settings "$HOME/.claude/tg-agent.settings.json" \
  --channels plugin:telegram@claude-plugins-official --permission-mode "${TG_PERMISSION_MODE:-auto}"
