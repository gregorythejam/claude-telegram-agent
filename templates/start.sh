#!/bin/bash
# Запуск Telegram-агента (служба tg-agent: systemctl --user status tg-agent).
# Продолжает прошлый разговор бота; после команды /new начинает новый.
WORK="${TELEGRAM_AGENT_WORK:-$HOME/telegram-agent}"
cd "$WORK" || exit 1
PROJ="$HOME/.claude/projects/$(printf '%s' "$WORK" | sed 's/[^A-Za-z0-9]/-/g')"
# Разговор бота — самый свежий журнал с сообщениями из Telegram. В той же папке могут быть
# и обычные сессии человека (VS Code, терминал): их бот подхватывать не должен.
RESUME=""
if [ ! -f "$TELEGRAM_STATE_DIR/new-session.flag" ]; then
  for f in $(ls -t "$PROJ"/*.jsonl 2>/dev/null); do
    if grep -qF '"content":"<channel source=\"plugin:telegram' "$f"; then
      RESUME="--resume $(basename "$f" .jsonl)"
      break
    fi
  done
fi
exec "$HOME/.local/bin/claude" $RESUME --settings "$HOME/.claude/tg-agent.settings.json" \
  --channels plugin:telegram@claude-plugins-official --permission-mode "${TG_PERMISSION_MODE:-auto}"
