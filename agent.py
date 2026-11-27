#!/usr/bin/env python3
"""Служебные команды Telegram-агента. Их вызывает плагин (команды из меню бота) и ночной таймер.

  agent.py now        что бот делает сейчас (команда /now)
  agent.py stop       прервать текущую работу (команда /stop)
  agent.py usage      расход лимитов подписки Claude (команда /usage)
  agent.py restart    перезапуститься с тем же разговором и прислать «✅ Перезапустился»
  agent.py night      ночная уборка: напоминание про /new при большом разговоре,
                      при TG_NIGHT_REVISE=1 ещё и ревизия дел по архиву переписки
  agent.py status     размер разговора и простой (для проверки)

Настройки берутся из окружения службы бота (значения по умолчанию — как ставит INSTALL.md):
  TELEGRAM_SERVICE      имя службы и tmux-сессии          tg-agent
  TELEGRAM_STATE_DIR    папка плагина (токен, доступ)     ~/.claude/channels/telegram
  TELEGRAM_AGENT_WORK   рабочая папка агента              ~/telegram-agent
  TELEGRAM_ARCHIVE_DIR  архив переписки                   <рабочая папка>/архив-переписки
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

H = Path.home()
SERVICE = os.environ.get("TELEGRAM_SERVICE", "tg-agent")
STATE = Path(os.environ.get("TELEGRAM_STATE_DIR", H / ".claude/channels/telegram"))
WORK = Path(os.environ.get("TELEGRAM_AGENT_WORK", H / "telegram-agent"))
ARCHIVE = Path(os.environ.get("TELEGRAM_ARCHIVE_DIR", WORK / "архив-переписки"))
CLAUDE = shutil.which("claude") or str(H / ".local/bin/claude")
LOG = H / ".local/state/telegram-agent.log"

CTX_LIMIT = 150_000            # токенов в запросе: больше — пора нажать /new
REMIND_EVERY = 3 * 24 * 3600   # напоминать не чаще раза в три дня
REMIND_TEXT = ("Наш разговор сильно разросся. Когда начнёте новую тему, нажмите /new: "
               "я начну с чистого листа и буду отвечать быстрее, а лимиты подписки "
               "будут тратиться медленнее. Всё важное я сохранил и найду, если вернётесь к старой теме.")


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


# ---------- Telegram ----------

def creds():
    token = ""
    for line in (STATE / ".env").read_text().splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            token = line.split("=", 1)[1].strip()
    owner = json.loads((STATE / "access.json").read_text())["allowFrom"][0]
    return token, str(owner)


def send(text, silent=False):
    token, owner = creds()
    data = urllib.parse.urlencode({"chat_id": owner, "text": text,
                                   "disable_notification": "true" if silent else "false"}).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30) as r:
        r.read()


# ---------- сессия ----------

def proj_dir():
    return H / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(WORK))


def claude_pid():
    """pid процесса claude внутри tmux службы бота."""
    out = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", SERVICE],
                         capture_output=True, text=True).stdout.strip()
    tmux = int(out or 0)
    if not tmux:
        return None
    for kid in subprocess.run(["pgrep", "-P", str(tmux)], capture_output=True, text=True).stdout.split():
        try:
            if Path(f"/proc/{kid}/comm").read_text().strip() == "claude":
                return int(kid)
        except OSError:
            pass
    return None


def current_session():
    """Журнал текущего разговора. Если Claude не сказал, какой, — самый свежий в папке агента."""
    pid = claude_pid()
    if pid:
        f = H / f".claude/sessions/{pid}.json"
        try:
            p = proj_dir() / f"{json.loads(f.read_text())['sessionId']}.jsonl"
            if p.exists():
                return p
        except (OSError, ValueError, KeyError):
            pass
    files = sorted(proj_dir().glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def tail_lines(path, size=4_000_000):
    with path.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - size))
        data = f.read()
    return data.decode("utf-8", "ignore").splitlines()


def context_info(path):
    """(токенов в последнем запросе, секунд с последнего сообщения)."""
    ctx, last = 0, None
    for line in reversed(tail_lines(path)):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") not in ("user", "assistant"):
            continue
        last = last or d.get("timestamp")
        u = (d.get("message") or {}).get("usage") if d.get("type") == "assistant" else None
        if u:
            ctx = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                   + u.get("cache_creation_input_tokens", 0))
            break
    idle = None
    if last:
        idle = time.time() - datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp()
    return ctx, idle


def local(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()


# ---------- /now и /stop ----------

CHANNEL = re.compile(r"<channel ([^>]*)>\n?(.*?)\n?</channel>", re.S)
SPINNER = re.compile(r"^\s*\S\s+\S+…\s+\((\d+[hms](?: \d+[ms])*)")


def pane():
    return subprocess.run(["tmux", "-L", SERVICE, "capture-pane", "-p", "-t", SERVICE],
                          capture_output=True, text=True).stdout


def is_busy(screen):
    tail = [l for l in screen.splitlines() if l.strip()][-3:]
    return any("esc to interrupt" in l for l in tail)


def is_channel(d):
    c = (d.get("message") or {}).get("content") if isinstance(d.get("message"), dict) else None
    return d.get("type") == "user" and isinstance(c, str) and c.startswith('<channel source="plugin:telegram')


def step_text(x):
    n, inp = x.get("name", ""), x.get("input") or {}
    base = lambda k: Path(str(inp.get(k, ""))).name
    if n == "Bash":
        return inp.get("description") or "команда в терминале"
    if n == "Read":
        return f"читаю {base('file_path')}"
    if n in ("Edit", "Write", "NotebookEdit"):
        return f"правлю {base('file_path') or base('notebook_path')}"
    if n.endswith("download_attachment"):
        return "скачиваю вложение"
    if n.endswith("telegram__reply"):
        return "пишу вам в Telegram"
    if n in ("Agent", "Task"):
        return f"помощник: {inp.get('description', '')}"
    if n == "WebSearch":
        return f"ищу в интернете: {inp.get('query', '')}"
    if n == "WebFetch":
        return "открываю страницу"
    if n.endswith("telegram__react") or n == "ToolSearch":
        return None
    return n.replace("mcp__", "")


def recent_steps(path, limit=4):
    """Последние шаги и сообщение из Telegram, над которым идёт работа."""
    steps, task = [], None
    for line in reversed(tail_lines(path, 2_000_000)):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if is_channel(d):
            m = CHANNEL.search(d["message"]["content"])
            task = " ".join((m.group(2) if m else "").split()) or "(вложение)"
            break
        if d.get("type") == "assistant" and isinstance((d.get("message") or {}).get("content"), list):
            for x in reversed(d["message"]["content"]):
                if x.get("type") == "tool_use" and len(steps) < limit:
                    t = step_text(x)
                    if t:
                        steps.append((d.get("timestamp"), t))
    return list(reversed(steps)), task


def cmd_now():
    screen = pane()
    if not screen.strip():
        return "Бот сейчас не запущен."
    lines = screen.splitlines()
    sess = current_session()
    tail = lines[-15:]
    tip = any("Teach auto mode" in l for l in tail)    # подсказка Claude Code, сообщениям не мешает
    if not tip and any("Enter to confirm" in l or "Do you want to" in l for l in tail):
        head = "⏸ Стою на вопросе в терминале, жду ответа там."
    elif is_busy(screen):
        dur = next((SPINNER.match(l).group(1) for l in reversed(lines) if SPINNER.match(l)), None)
        head = "🟢 Работаю" + (" уже " + dur.replace("h", " ч").replace("m", " мин").replace("s", " с") + "." if dur else ".")
    else:
        idle = context_info(sess)[1] if sess else None
        head = "💤 Свободен, жду сообщения." + (f" Последнее действие {int(idle // 60)} мин назад." if idle else "")
    if not sess:
        return head
    steps, task = recent_steps(sess)
    out = [head]
    if task:
        out.append(f"\nЗадача: «{task[:150]}{'…' if len(task) > 150 else ''}»")
    if steps:
        out.append("\nПоследние шаги:")
        out += [f"• {local(ts):%H:%M} {t[:120]}" for ts, t in steps if ts]
    return "\n".join(out)


def cmd_stop():
    screen = pane()
    if not screen.strip():
        return "Бот сейчас не запущен."
    if not is_busy(screen):
        return "Я сейчас ничего не делаю, останавливать нечего."
    subprocess.run(["tmux", "-L", SERVICE, "send-keys", "-t", SERVICE, "Escape"])
    log("/stop — прервал работу")
    for _ in range(10):
        time.sleep(0.5)
        if "Interrupted" in pane():
            return ("⏹ Остановил. Если вы успели написать что-то ещё, пока я работал, "
                    "возьмусь за это сейчас. Если не нужно, нажмите /stop ещё раз.")
    return "Нажал «стоп», но не уверен, что сработало. Посмотрите /now через пару секунд."


# ---------- /usage ----------

RESETS = re.compile(r"Resets\s+(?:(\w{3}) (\d{1,2}), )?(\d{1,2})(?::(\d\d))?(am|pm)\s*\(([^)]+)\)")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def resets_local(text):
    """«Resets 3pm (UTC)» → «сброс в 18:00» по местному времени бота."""
    m = RESETS.search(text)
    if not m:
        return text.strip()
    mon, day, hh, mm, ap, tz = m.groups()
    if tz != "UTC":
        return text.strip()
    h = int(hh) % 12 + (12 if ap == "pm" else 0)
    now = datetime.now(timezone.utc)
    if mon:
        dt = datetime(now.year, MONTHS.index(mon) + 1, int(day), h, int(mm or 0), tzinfo=timezone.utc)
        if dt < now - timedelta(days=1):
            dt = dt.replace(year=now.year + 1)
        return f"сброс {dt.astimezone():%d.%m в %H:%M}"
    dt = now.replace(hour=h, minute=int(mm or 0), second=0, microsecond=0)
    if dt < now:
        dt += timedelta(days=1)
    return f"сброс в {dt.astimezone():%H:%M}"


def cmd_usage():
    """Открывает отдельный Claude без хуков и каналов, читает /usage и закрывает его."""
    d = H / ".local/state/telegram-agent-usage"
    d.mkdir(parents=True, exist_ok=True)
    sock = f"tg-usage-{os.getpid()}"
    tm = lambda *a: subprocess.run(["tmux", "-L", sock, *a], capture_output=True, text=True).stdout
    tm("new-session", "-d", "-s", "u", "-x", "120", "-y", "60", "-c", str(d),
       f"{CLAUDE} --settings '{{\"disableAllHooks\": true}}'")
    scr = ""
    try:
        for _ in range(40):
            time.sleep(0.5)
            scr = tm("capture-pane", "-p", "-t", "u")
            if "Yes, I trust this folder" in scr:      # первый запуск в этой папке
                tm("send-keys", "-t", "u", "Down", "Enter")
                time.sleep(1)
            elif "❯" in scr:
                break
        tm("send-keys", "-t", "u", "/usage")
        time.sleep(0.7)
        tm("send-keys", "-t", "u", "Enter")
        for _ in range(40):
            time.sleep(0.5)
            scr = tm("capture-pane", "-p", "-t", "u", "-S", "-80")
            if re.search(r"Current week.*\n.*\d+% used\s*\n\s*Resets", scr):
                break
    finally:
        tm("kill-server")
    lines = [l.strip() for l in scr.splitlines()]
    out = []
    for i, l in enumerate(lines):
        if not l.startswith("Current "):
            continue
        pct = next((re.search(r"(\d+)% used", x).group(1) for x in lines[i + 1:i + 4] if re.search(r"\d+% used", x)), None)
        res = next((resets_local(x) for x in lines[i + 1:i + 5] if x.startswith("Resets")), "")
        if pct is None:
            continue
        what = l[len("Current "):]
        label = {"session": "5 часов", "week (all models)": "Неделя"}.get(what, "Неделя, " + what.replace("week ", "").strip("()"))
        out.append(f"{label}: {pct}%" + (f", {res}" if res else ""))
    if not out:
        log(f"/usage: не разобрал экран: {scr[-500:]!r}")
        return "Не получилось прочитать лимиты. Посмотрите на claude.ai → Settings → Usage."
    return "📊 Лимиты Claude\n" + "\n".join(out)


# ---------- перезапуск ----------

def cmd_restart():
    """Перезапуск службы с тем же разговором. После старта плагин пришлёт «✅ Перезапустился»."""
    _, owner = creds()
    flag = STATE / "restart.flag"
    flag.write_text(json.dumps({"chat_id": owner, "at": time.time()}))
    os.chmod(flag, 0o600)
    subprocess.run(["systemd-run", "--user", "--collect", "--quiet", "--on-active=10",
                    "systemctl", "--user", "restart", SERVICE], check=True)
    log("перезапуск по просьбе")
    return "Перезапускаюсь через 10 секунд."


# ---------- ночная уборка ----------

REVISE = """Ты — ночной ревизор памяти Telegram-агента. Работаешь без собеседника, только с файлами.

Задача: всё важное из переписки за сутки должно лежать в файлах, а не только в чате. Агент может начать разговор с чистого листа и будет помнить только файлы.

1. Прочитай новую переписку: {pending}
2. Важное хранится в {work}/дела/<тема>/дело.md (одно дело на тему). Прочитай существующие дела.
3. Найди в переписке важное, чего в делах ещё нет или что изменилось: факты, даты, суммы, имена, решения, договорённости, отправленные документы, сроки, что делать дальше. Мелочи, приветствия и технические подробности работы бота не нужны.
4. Внеси это кратко в нужный файл. Устаревшее замени, а не оставляй рядом. Новую тему — новым делом по образцу существующих. Если сомневаешься, важно ли, — не пиши, а упомяни в отчёте.
5. Архив переписки ({archive}) не меняй.

Отчёт читает владелец с телефона. Ответь только строками такого вида, без вступлений и заключений:
+ Тема — что запомнил
? Что осталось под вопросом
Правила отчёта: строка «+» на каждую записанную тему (не больше трёх), строка «?» только если важное не записал из-за сомнений (не больше одной); каждая строка короче 80 знаков, простыми словами, без имён файлов. Если ничего не записал и вопросов нет — ответь одним словом: пусто."""


def pending_text():
    """Новая часть архива с прошлой ревизии: (текст, новое состояние)."""
    stf = ARCHIVE / ".ревизия.json"
    st = json.loads(stf.read_text()) if stf.exists() else {}
    parts = []
    for f in sorted(ARCHIVE.glob("????-??-??.md")):
        data = f.read_bytes()
        off = st.get(f.name, 0)
        if len(data) > off:
            parts.append(data[off:].decode("utf-8", "ignore"))
            st[f.name] = len(data)
    return "\n".join(parts).strip(), st


def revise():
    text, st = pending_text()
    if not text:
        return None
    pending = ARCHIVE / ".к-ревизии.md"
    pending.write_text(text)
    os.chmod(pending, 0o600)
    cmd = [CLAUDE, "-p", REVISE.format(pending=pending, work=WORK, archive=ARCHIVE),
           "--model", "sonnet", "--no-session-persistence",
           "--tools", "Read,Write,Edit,Glob,Grep", "--permission-mode", "acceptEdits",
           "--settings", '{"disableAllHooks": true}', "--strict-mcp-config", "--add-dir", str(ARCHIVE)]
    r = subprocess.run(cmd, cwd=WORK, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"ревизия не удалась: {(r.stderr or r.stdout)[-300:]}")
    (ARCHIVE / ".ревизия.json").write_text(json.dumps(st))
    pending.unlink()
    lines = []
    for line in r.stdout.splitlines():
        line = line.strip().replace("`", "")
        if not line or line.lower().strip(".") == "пусто":
            continue
        mark = "❓ " if line[0] == "?" else "• "
        lines.append(mark + line.lstrip("+?-•* ").strip()[:160])
    return lines


def cmd_night():
    out = []
    if os.environ.get("TG_NIGHT_REVISE") == "1":
        try:
            lines = revise()
            if lines:
                out.append("🌙 Ночная уборка: запомнил из сегодняшней переписки\n" + "\n".join(lines))
        except Exception as e:
            log(f"ночь: {e!r}")
            out.append(f"⛔ Ночная уборка не удалась: {str(e)[:200]}")
    sess = current_session()
    stamp = STATE / "new-reminded.at"
    if sess and context_info(sess)[0] > CTX_LIMIT:
        if not stamp.exists() or time.time() - stamp.stat().st_mtime > REMIND_EVERY:
            out.append(REMIND_TEXT)
            stamp.touch()
    if out:
        send("\n\n".join(out), silent=True)
    log("ночь: " + (" | ".join(out).replace("\n", " ") or "нечего сообщать"))
    return "\n\n".join(out) or "Нечего сообщать."


def cmd_status():
    sess = current_session()
    if not sess:
        return "Разговора ещё нет."
    ctx, idle = context_info(sess)
    return f"Разговор {sess.stem[:8]}: {ctx // 1000} тыс. токенов в последнем запросе, простой {int((idle or 0) // 60)} мин."


def main():
    os.umask(0o077)   # архив и отметки — только для владельца
    cmds = {"now": cmd_now, "stop": cmd_stop, "usage": cmd_usage, "restart": cmd_restart,
            "night": cmd_night, "status": cmd_status}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    print(cmds[sys.argv[1]]())


if __name__ == "__main__":
    main()
