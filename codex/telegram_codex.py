"""Личный Telegram-бот для Codex CLI. Python 3.10+, Linux.

  python3 telegram_codex.py setup          подключить бота: токен вводится скрыто, владелец — кто первым напишет
  python3 telegram_codex.py                работа бота (так его запускает служба tg-codex)
  python3 telegram_codex.py send ИМЯ       отправить файл из папки отправки владельцу

Бот принимает текст, фото (и альбомы), файлы, голосовые (если установлена команда tg-voice),
пересланные посты, опросы и геоточки. Каждое сообщение передаёт в `codex exec`, продолжая
разговор, пока владелец не нажмёт /new. Команды /now, /stop, /usage работают, даже когда Codex занят.
"""
import fcntl
import getpass
import http.client
import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from bisect import bisect_right
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

try:
    from markdown_it import MarkdownIt
except ModuleNotFoundError:   # без модуля ответы уходят простым текстом
    MarkdownIt = None

API = os.getenv("TG_API", "https://api.telegram.org")   # подмена — только для проверок
CONFIG = Path.home() / ".config/tg-codex"
STATE = Path.home() / ".local/state/tg-codex"
WORK = Path(os.getenv("TG_CODEX_WORKDIR", str(Path.home() / "codex-agent")))
ARCHIVE = WORK / "архив-переписки"
OUTGOING = STATE / "outbox"      # файлы, которые Codex готовит к отправке; удаляются после отправки
INBOX = STATE / "inbox"          # что прислал владелец; старше недели удаляется
CODEX_HOME = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
TIMEOUT = int(os.getenv("TG_CODEX_TIMEOUT", "1800"))
MAX_UPLOAD = 50 * 1024 * 1024
MEDIA = {".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".gif": "animation",
         ".mp4": "video", ".mp3": "audio", ".m4a": "audio"}
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
COMMANDS = [("new", "Начать новый диалог"), ("now", "Что я сейчас делаю"),
            ("stop", "Остановить текущую работу"), ("today", "Итоги дня"),
            ("usage", "Расход лимитов ChatGPT")]


def save(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        out.write(value)
        out.flush()
        os.fsync(out.fileno())
    os.replace(out.name, path)


# ---------- Telegram ----------

class TelegramError(Exception):
    def __init__(self, code, delay=5):
        self.code = code
        self.delay = delay
        super().__init__(f"Telegram HTTP {code}")


def telegram(token, method, *, request_timeout=45, **params):
    return telegram_request(token, method, json.dumps(params).encode(), "application/json", request_timeout)


def telegram_request(token, method, data, content_type, request_timeout):
    request = Request(f"{API}/bot{token}/{method}", data=data, headers={"Content-Type": content_type})
    try:
        with urlopen(request, timeout=request_timeout) as response:
            return json.load(response)["result"]
    except HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except ValueError:
            body = {}
        raise TelegramError(exc.code, body.get("parameters", {}).get("retry_after", 5)) from None
    except (OSError, URLError, ValueError, http.client.HTTPException):
        raise TelegramError(0) from None


def download(token, file_id, dest):
    """Скачать вложение в dest. Bot API отдаёт файлы до 20 МБ."""
    info = telegram(token, "getFile", file_id=file_id)
    with urlopen(f"{API}/file/bot{token}/{info['file_path']}", timeout=120) as source, open(dest, "wb") as out:
        shutil.copyfileobj(source, out)
    return dest


# ---------- разметка ответа ----------
# Telegram считает смещения разметки в UTF-16, большинство эмодзи занимают две единицы.

MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("strikethrough") if MarkdownIt else None
INLINE_STYLES = {"strong_open": "bold", "em_open": "italic", "s_open": "strikethrough"}
REPLY_STYLE = (
    "Ответ читают в Telegram. Если пользователь не просил другой формат, "
    "разделяй мысли пустой строкой, шаги оформляй нумерованным списком, "
    "ключевые названия выделяй жирным, пути заключай в обратные кавычки. "
    "Команды для копирования помещай в отдельный блок кода без приглашения терминала. "
    "Для сравнения предпочитай короткие списки таблицам. "
    f"Когда пользователь просит прислать файл, помести готовый файл в папку {OUTGOING} "
    "под уникальным именем, в рабочей папке копию не оставляй. Для каждого вложения добавь в финальный ответ "
    "отдельный абзац TG_FILE имя_файла с настоящим именем без пути, кавычек и разметки. "
    "Бот сам загрузит эти файлы в этот чат.\n\n"
)


def styled_inline(tokens, base):
    styles = [base]
    local_path = ""
    label = []
    for token in tokens:
        kind = token.type
        if kind in INLINE_STYLES:
            style = styles[-1]
            styles.append(style if style and style["type"] == "text_link"
                          else {"type": INLINE_STYLES[kind]})
        elif kind == "link_open":
            url = token.attrGet("href") or ""
            public = url.startswith(("https://", "http://", "tg://", "mailto:"))
            local_path = unquote(url) if url.startswith(("/", "./", "../", "~/")) else ""
            label = []
            styles.append({"type": "text_link", "url": url} if public else {"type": "bold"})
        elif kind in ("strong_close", "em_close", "s_close", "link_close"):
            styles.pop()
            if kind == "link_close" and local_path:
                if local_path != "".join(label):
                    yield " (", None
                    yield local_path, {"type": "code"}
                    yield ")", None
                local_path = ""
        elif kind in ("softbreak", "hardbreak"):
            yield "\n", None
        elif kind == "code_inline":
            if local_path:
                label.append(token.content)
            yield token.content, styles[-1] if styles[-1] and styles[-1]["type"] == "text_link" else {"type": "code"}
        elif kind == "image":
            yield token.content or token.attrGet("src") or "", styles[-1]
        else:
            if local_path:
                label.append(token.content)
            yield token.content, styles[-1]


def telegram_runs(markdown):
    if not MARKDOWN:
        yield markdown, None
        return
    lists = []
    prefix = ""
    heading = False
    first_block = True
    previous_in_list = False
    for token in MARKDOWN.parse(markdown):
        kind = token.type
        if kind in ("bullet_list_open", "ordered_list_open"):
            lists.append(int(token.attrGet("start") or 1) if kind == "ordered_list_open" else None)
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif kind == "list_item_open":
            number = lists[-1]
            marker = "• " if number is None else f"{number}. "
            prefix = "  " * (len(lists) - 1) + marker
            if number is not None:
                lists[-1] += 1
        elif kind in ("heading_open", "heading_close"):
            heading = kind == "heading_open"
        elif kind in ("inline", "fence", "code_block", "hr"):
            if not first_block:
                yield ("\n" if previous_in_list and lists and prefix else "\n\n"), None
            first_block = False
            previous_in_list = bool(lists)
            if prefix:
                yield prefix, None
                prefix = ""
            if kind == "inline":
                yield from styled_inline(token.children or [], {"type": "bold"} if heading else None)
            elif kind == "hr":
                yield "────", None
            else:
                style = {"type": "pre"}
                language = token.info.split()[:1]
                if language and re.fullmatch(r"[\w.+#-]{1,32}", language[0]):
                    style["language"] = language[0]
                yield token.content.removesuffix("\n"), style


def format_reply(markdown):
    parts = []
    spans = []
    length = 0
    for value, style in telegram_runs(markdown):
        if not value:
            continue
        end = length + len(value)
        if style:
            if spans and spans[-1][1] == length and spans[-1][2] == style:
                spans[-1] = (spans[-1][0], end, style)
            else:
                spans.append((length, end, style))
        parts.append(value)
        length = end
    text = "".join(parts)
    if not text.strip():
        text = markdown.strip() or "Пустой ответ."
        spans = []
    units = [0]
    for char in text:
        units.append(units[-1] + (2 if ord(char) > 0xffff else 1))

    messages = []
    start = 0
    span_index = 0
    while start < len(text):
        while span_index < len(spans) and spans[span_index][1] <= start:
            span_index += 1
        end = bisect_right(units, units[start] + 4096) - 1
        # Короткие команды не разрываем между сообщениями: их удобно копировать целиком.
        for left, right, style in spans[span_index:span_index + 100]:
            if left >= end:
                break
            if style["type"] == "pre" and start < left < end < right and units[right] - units[left] <= 4096:
                end = left
                break
        if span_index + 100 < len(spans):
            end = min(end, spans[span_index + 100][0])
        if end < len(text) and (end == 0 or text[end - 1] != "\n"):
            for separator in ("\n\n", "\n", " "):
                pos = text.rfind(separator, start + (end - start) // 2, end)
                if pos >= 0:
                    end = pos + len(separator)
                    break
        message = {"text": text[start:end]}
        entities = []
        idx = span_index
        while idx < len(spans) and spans[idx][0] < end:
            left, right, style = spans[idx]
            a, b = max(start, left), min(end, right)
            entities.append(dict(style, offset=units[a] - units[start], length=units[b] - units[a]))
            idx += 1
        if entities:
            message["entities"] = entities
        if message["text"].strip():
            messages.append(message)
        start = end
    return messages


def outgoing_reply(answer):
    """Ответ Codex → сообщения и вложения. Вложение — отдельный абзац «TG_FILE имя»."""
    if not MARKDOWN:
        out, text = [], []
        for line in answer.splitlines():
            m = re.fullmatch(r"TG_FILE ([^\r\n]+)", line.strip())
            if m:
                if "\n".join(text).strip():
                    out += format_reply("\n".join(text).strip())
                out.append({"attachment": m[1]})
                text = []
            else:
                text.append(line)
        return out + (format_reply("\n".join(text).strip()) if "\n".join(text).strip() else [])
    lines = answer.splitlines(keepends=True)
    messages = []
    start = 0
    for token in MARKDOWN.parse(answer):
        if token.type != "inline" or token.level != 1 or not token.map:
            continue
        first, end = token.map
        if end != first + 1:
            continue
        match = re.fullmatch(r"TG_FILE ([^\r\n]+)", lines[first].strip())
        if match:
            before = "".join(lines[start:first]).strip()
            if before:
                messages.extend(format_reply(before))
            messages.append({"attachment": match[1]})
            start = end
    if not messages:
        return format_reply(answer)
    tail = "".join(lines[start:]).strip()
    return messages + (format_reply(tail) if tail else [])


def read_attachment(name):
    if (not name or name.startswith(".") or "/" in name or "\\" in name
            or not name.isprintable()):
        raise ValueError("Недопустимое имя вложения.")
    # Открываем обычный файл прямо в папке отправки; ни папка, ни файл не могут быть ссылкой.
    directory = os.open(OUTGOING, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Вложение должно быть обычным файлом.")
        if info.st_size > MAX_UPLOAD:
            raise ValueError("Файл превышает лимит загрузки 50 МБ.")
        data = source.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise ValueError("Файл превышает лимит загрузки 50 МБ.")
    if not data:
        raise ValueError("Файл пуст.")
    return data


def send_attachment(token, owner, name):
    data = read_attachment(name)
    field = MEDIA.get(Path(name).suffix.lower(), "document")
    if field == "photo" and len(data) > 10 * 1024 * 1024:
        field = "document"
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    boundary = uuid4().hex
    filename = name.replace('"', "%22")

    def upload(field):
        prefix = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{owner}\r\n"
                  f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                  f"filename=\"{filename}\"\r\nContent-Type: {mime}\r\n\r\n")
        body = b"".join((prefix.encode(), data, f"\r\n--{boundary}--\r\n".encode()))
        telegram_request(token, "send" + field.capitalize(), body,
                         "multipart/form-data; boundary=" + boundary, 180)

    try:
        upload(field)
    except TelegramError as exc:
        if exc.code != 400 or field == "document":
            raise
        upload("document")


def send_reply(token, owner, message):
    payload = {"text": message} if isinstance(message, str) else dict(message)
    if "attachment" in payload:
        try:
            send_attachment(token, owner, payload["attachment"])
            (OUTGOING / payload["attachment"]).unlink(missing_ok=True)
            return
        except ValueError as exc:
            payload = {"text": str(exc)}
        except OSError:
            payload = {"text": "Не удалось открыть вложение из папки отправки."}
        except TelegramError as exc:
            if exc.code not in (400, 413):
                raise
            payload = {"text": "Telegram отклонил загрузку файла."}
    try:
        telegram(token, "sendMessage", chat_id=owner, **payload)
    except TelegramError as exc:
        if exc.code != 400 or not payload.get("entities"):
            raise
        telegram(token, "sendMessage", chat_id=owner, text=payload["text"])


# ---------- архив переписки ----------

def archive(who, text):
    """Дописать запись в архив-переписки/ГГГГ-ММ-ДД.md (местное время)."""
    try:
        now = datetime.now()
        ARCHIVE.mkdir(parents=True, exist_ok=True, mode=0o700)
        day = ARCHIVE / f"{now:%Y-%m-%d}.md"
        head = "" if day.exists() else f"# Переписка {now:%d.%m.%Y}\n"
        with day.open("a") as f:
            f.write(f"{head}\n**{now:%H:%M} · {who}**\n{text.strip()}\n")
    except OSError as exc:
        print(f"архив: {exc}", file=sys.stderr, flush=True)


# ---------- настройка ----------

def setup():
    if not sys.stdin.isatty():
        sys.exit("Запустите настройку в терминале: python3 telegram_codex.py setup")
    service = Path.home() / ".config/systemd/user/tg-codex.service"
    if service.exists():   # пока идёт настройка, бота не должен опрашивать никто другой
        subprocess.run(["systemctl", "--user", "stop", "tg-codex.service"])
    token = getpass.getpass("Вставьте токен бота из @BotFather и нажмите Enter (символы не видны): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        sys.exit("Это не похоже на токен. Скопируйте его из @BotFather целиком и запустите настройку ещё раз.")
    try:
        bot = telegram(token, "getMe")
    except TelegramError as exc:
        sys.exit(f"Telegram не принял токен ({exc}). Проверьте его в @BotFather.")
    if telegram(token, "getWebhookInfo").get("url"):
        sys.exit("У этого бота настроен webhook, им пользуется другая программа. Создайте отдельного бота.")
    print(f"Бот @{bot['username']} найден.")
    print(f"Теперь откройте https://t.me/{bot['username']} в Telegram и напишите ему «привет». Жду 3 минуты…")
    offset, owner = 0, None
    deadline = time.time() + 180
    while time.time() < deadline and not owner:
        try:
            updates = telegram(token, "getUpdates", offset=offset, timeout=20, allowed_updates=["message"])
        except TelegramError as exc:
            if exc.code == 409:
                sys.exit("Этого бота уже опрашивает другая программа. Остановите её или создайте нового бота.")
            time.sleep(3)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            m = u.get("message", {})
            if m.get("chat", {}).get("type") == "private" and m.get("from"):
                f = m["from"]
                name = " ".join(x for x in (f.get("first_name"), f.get("last_name")) if x)
                if f.get("username"):
                    name += f" (@{f['username']})"
                ans = input(f"Пришло сообщение от: {name}. Это вы? [д/н] ").strip().lower()
                if ans in ("", "д", "да", "y", "yes"):
                    owner = f["id"]
                    break
    if not owner:
        sys.exit("Сообщение не пришло. Запустите настройку ещё раз и напишите боту.")
    telegram(token, "getUpdates", offset=offset, timeout=0)   # не отвечать на «привет» повторно
    save(CONFIG / "token", token)
    save(CONFIG / "owner", str(owner))
    telegram(token, "sendMessage", chat_id=owner, text="✅ Готово: теперь я отвечаю только вам.")
    if service.exists():
        subprocess.run(["systemctl", "--user", "restart", "tg-codex.service"], check=True)
        print("Бот запущен. Напишите ему что-нибудь в Telegram.")
    else:
        print("Токен и владелец сохранены.")


# ---------- Codex ----------

def codex_binary():
    installed = shutil.which("codex")
    if installed:
        return installed
    configured = CONFIG / "codex-path"
    if configured.exists():
        path = Path(configured.read_text().strip())
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    # На сервере без терминала Codex может быть только внутри расширения VS Code.
    candidates = (Path.home() / ".vscode-server/extensions").glob("openai.chatgpt-*/bin/*/codex")
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        if os.access(path, os.X_OK):
            return str(path)
    raise RuntimeError("Codex CLI не найден. Проверьте ~/.config/tg-codex/codex-path.")


class Stopped(Exception):
    pass


CURRENT = {"proc": None, "stopped": False}   # процесс Codex, который сейчас работает


def run_codex(prompt, thread, images=()):
    cmd = [codex_binary(), "-a", "never", "exec", "-s", "workspace-write",
           "--json", "--skip-git-repo-check", "--add-dir", str(OUTGOING)]
    if thread:
        cmd.extend(["resume", str(UUID(thread))])
    for image in images:
        cmd.extend(["--image", image])
    if images:
        cmd.append("--")
    with tempfile.TemporaryFile(mode="w+") as events:
        with subprocess.Popen(cmd + ["-"], cwd=WORK, stdin=subprocess.PIPE, stdout=events,
                              stderr=subprocess.DEVNULL, text=True, start_new_session=True) as proc:
            CURRENT.update(proc=proc, stopped=False)
            try:
                proc.communicate(REPLY_STYLE + prompt, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise RuntimeError(f"Задача не уложилась в {TIMEOUT // 60} минут, я её остановил.")
            finally:
                CURRENT["proc"] = None
        events.seek(0)
        answer = ""
        completed = False
        for line in events:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "thread.started":
                thread = str(UUID(event["thread_id"]))
            elif event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    answer = item["text"]
            elif event.get("type") == "turn.completed":
                completed = True
        if CURRENT["stopped"]:
            raise Stopped(thread)
        if proc.returncode or not completed or not answer.strip() or not thread:
            raise RuntimeError("Codex не завершил ответ. Проверьте вход (codex login) и доступный лимит.")
        return thread, answer


def stop_codex():
    proc = CURRENT["proc"]
    if not proc or proc.poll() is not None:
        return False
    CURRENT["stopped"] = True
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        for _ in range(20):
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def usage_text():
    """Лимиты ChatGPT из последней записи Codex о расходе (данные последнего запуска)."""
    files = sorted((CODEX_HOME / "sessions").glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:5]
    for f in files:
        for line in reversed(f.read_text(errors="ignore").splitlines()[-400:]):
            if '"rate_limits"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rl = (d.get("payload") or {}).get("rate_limits")
            if not rl:
                continue
            out = []
            for key in ("primary", "secondary"):
                w = rl.get(key)
                if not w:
                    continue
                mins = w.get("window_minutes") or 0
                label = {300: "5 часов", 10080: "Неделя"}.get(mins, f"{round(mins / 60)} ч")
                s = f"{label}: {round(w.get('used_percent', 0))}%"
                if w.get("resets_at"):
                    s += f", сброс {datetime.fromtimestamp(w['resets_at']):%d.%m в %H:%M}"
                out.append(s)
            if not out:
                continue
            when = d.get("timestamp")
            when = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone() if when else None
            return ("📊 Лимиты ChatGPT\n" + "\n".join(out)
                    + (f"\n\nПо данным последней задачи, {when:%d.%m %H:%M}." if when else ""))
    return "Пока не знаю: данные о лимитах появятся после первой задачи."


@contextmanager
def typing_indicator(token, chat_id):
    stopped = threading.Event()

    def refresh():
        while not stopped.is_set():
            next_refresh = time.monotonic() + 4
            try:
                telegram(token, "sendChatAction", chat_id=chat_id, action="typing", request_timeout=2)
            except TelegramError as exc:
                if exc.code in (400, 401, 403, 404):
                    return
                if exc.code == 429:
                    next_refresh = max(next_refresh, time.monotonic() + exc.delay)
            stopped.wait(max(0, next_refresh - time.monotonic()))

    thread = threading.Thread(target=refresh, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()


# ---------- разбор входящих ----------

def safe_name(name):
    name = re.sub(r"[^\w.\-]+", "_", name or "")[:80].strip("._")
    return name or "file"


def forward_note(m):
    o = m.get("forward_origin")
    if not o:
        return ""
    if o.get("type") == "channel":
        chat = o.get("chat", {})
        who = f"@{chat['username']}" if chat.get("username") else chat.get("title", "канал")
        link = f", https://t.me/{chat['username']}/{o['message_id']}" if chat.get("username") and o.get("message_id") else ""
        return f"[Переслано из канала {who}{link}]\n"
    if o.get("type") == "user":
        u = o.get("sender_user", {})
        who = f"@{u['username']}" if u.get("username") else " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
        return f"[Переслано от {who or 'пользователя'}]\n"
    if o.get("type") == "hidden_user":
        return f"[Переслано от {o.get('sender_user_name', 'пользователя')}]\n"
    if o.get("type") == "chat":
        return f"[Переслано из чата {o.get('sender_chat', {}).get('title', '')}]\n"
    return "[Переслано]\n"


def describe_other(m):
    """Текстовое описание сообщений без текста и файлов (опрос, геоточка, контакт, стикер)."""
    if m.get("poll"):
        p = m["poll"]
        return "(опрос) " + p.get("question", "") + "\n" + "\n".join(
            f"- {o.get('text')} ({o.get('voter_count', 0)})" for o in p.get("options", []))
    if m.get("location"):
        return f"(геоточка {m['location']['latitude']}, {m['location']['longitude']})"
    if m.get("venue"):
        return f"(место: {m['venue'].get('title')}, {m['venue'].get('address')})"
    if m.get("contact"):
        c = m["contact"]
        return f"(контакт {c.get('first_name', '')} {c.get('last_name', '')} {c.get('phone_number', '')})".strip()
    if m.get("sticker"):
        return f"(стикер {m['sticker'].get('emoji', '')})"
    if m.get("dice"):
        return f"(кубик {m['dice'].get('emoji')} {m['dice'].get('value')})"
    return ""


def build_prompt(token, msgs, chat_id):
    """Одно или несколько сообщений (альбом) → (текст для Codex, картинки, запись для архива, ответ без Codex)."""
    INBOX.mkdir(mode=0o700, parents=True, exist_ok=True)
    texts, images, files, notes = [], [], [], []
    for m in msgs:
        fwd = forward_note(m)
        body = (m.get("text") or m.get("caption") or "").strip()
        try:
            if m.get("photo"):
                photo = max(m["photo"], key=lambda s: s["width"] * s["height"])
                path = INBOX / f"{m['message_id']}-photo.jpg"
                images.append(str(download(token, photo["file_id"], path)))
                notes.append(f"фото {path}")
            for kind in ("document", "video", "audio", "animation", "video_note"):
                if m.get(kind):
                    obj = m[kind]
                    if (obj.get("file_size") or 0) > 20 * 1024 * 1024:
                        return None, [], "", "Файл больше 20 МБ: Telegram не отдаёт ботам такие. Положите его на сервер другим способом."
                    name = safe_name(obj.get("file_name") or f"{kind}{mimetypes.guess_extension(obj.get('mime_type') or '') or ''}")
                    path = INBOX / f"{m['message_id']}-{name}"
                    download(token, obj["file_id"], path)
                    if path.suffix.lower() in IMAGES and kind == "document":
                        images.append(str(path))
                    files.append(str(path))
                    notes.append(f"{kind}: {name}")
            if m.get("voice"):
                tool = shutil.which("tg-voice")
                if not tool:
                    return None, [], "", "Голосовые пока не настроены. Напишите текстом, пожалуйста."
                path = download(token, m["voice"]["file_id"], INBOX / f"{m['message_id']}-voice.ogg")
                r = subprocess.run([tool, str(path)], capture_output=True, text=True, timeout=900)
                heard = r.stdout.strip()
                if r.returncode or not heard:
                    return None, [], "", "Не получилось расшифровать голосовое. Попробуйте ещё раз или напишите текстом."
                telegram(token, "sendMessage", chat_id=chat_id, text=f"🎙 «{heard}»",
                         reply_parameters={"message_id": m["message_id"]})
                body = (body + "\n" + heard).strip()
                notes.append("голосовое")
        except (TelegramError, OSError, KeyError, subprocess.TimeoutExpired):
            return None, [], "", "Не удалось получить вложение из Telegram. Пришлите его ещё раз."
        body = body or describe_other(m)
        if fwd or body:
            texts.append(fwd + body)
    prompt = "\n\n".join(t for t in texts if t.strip())
    if files:
        prompt += "\n\nПрислал файлы: " + ", ".join(files)
    if not prompt.strip():
        if images:
            prompt = "Опиши, что видно на фотографии." if len(images) == 1 else "Опиши, что видно на фотографиях."
        elif files:
            prompt = f"Посмотри присланный файл и коротко скажи, что в нём: {files[0]}"
        else:
            return None, [], "", "Такое сообщение я пока не понимаю. Напишите текстом."
    record = (f"[{'; '.join(notes)}]\n" if notes else "") + prompt
    return prompt, images, record, None


# ---------- работа бота ----------

class Bot:
    def __init__(self, token, owner):
        self.token, self.owner = token, owner
        self.lock = threading.Lock()
        self.tasks = queue.Queue()
        self.journal = STATE / "state.json"
        self.state = json.loads(self.journal.read_text()) if self.journal.exists() else {}
        self.groups = {}      # альбомы: media_group_id → [сообщения], ждём, пока придут все

    def commit(self):
        with self.lock:
            save(self.journal, json.dumps(self.state, ensure_ascii=False))

    def send(self, message):
        try:
            send_reply(self.token, self.owner, message)
        except TelegramError:
            with self.lock:
                self.state.setdefault("outbox", []).append(message)
            self.commit()

    def say(self, text):
        self.send({"text": text})

    # --- поток Codex: задачи по одной ---
    def worker(self):
        while True:
            msgs = self.tasks.get()
            label = " ".join(((msgs[0].get("text") or msgs[0].get("caption") or "") or "(вложение)").split())[:150]
            with self.lock:
                self.state["running"] = {"task": label, "since": time.time()}
                gen = self.state.get("gen", 0)
                thread = self.state.get("thread")
            self.commit()
            answer = None
            try:
                with typing_indicator(self.token, self.owner):
                    prompt, images, record, refusal = build_prompt(self.token, msgs, self.owner)
                    if refusal:
                        answer = refusal
                    else:
                        archive(msgs[0].get("from", {}).get("first_name") or "владелец", record)
                        new_thread, answer = run_codex(prompt, thread, images)
                        with self.lock:
                            if self.state.get("gen", 0) == gen:
                                self.state["thread"] = new_thread
                        archive("бот", answer)
            except Stopped as exc:
                with self.lock:
                    if exc.args and exc.args[0] and self.state.get("gen", 0) == gen:
                        self.state["thread"] = exc.args[0]
            except (RuntimeError, OSError, ValueError) as exc:
                print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                answer = str(exc) if isinstance(exc, RuntimeError) else \
                    "Codex не завершил запрос. Проверьте результат перед повтором."
            with self.lock:
                self.state.pop("running", None)
            self.commit()
            if answer:
                for part in outgoing_reply(answer):
                    self.send(part)

    # --- команды, которые работают сразу ---
    def command(self, cmd, msg):
        running = self.state.get("running")
        if cmd == "start":
            self.say("Я на связи. Напишите задачу, пришлите фото, файл или голосовое.\n\n"
                     "Команды в меню: /new — новый диалог, /now — что я делаю, /stop — остановить, "
                     "/usage — лимиты, /today — итоги дня.")
        elif cmd == "new":
            with self.lock:
                self.state.pop("thread", None)
                self.state["gen"] = self.state.get("gen", 0) + 1
            self.commit()
            self.say("✅ Новый диалог начат. Пишите." + (
                "\nТекущую задачу доделаю, но её итог в новый диалог не попадёт." if running else ""))
        elif cmd == "now":
            if not running:
                self.say("💤 Свободен, жду сообщения.")
            else:
                mins = int((time.time() - running["since"]) // 60)
                left = self.tasks.qsize()
                self.say(f"🟢 Работаю{f' уже {mins} мин' if mins else ''}.\n\nЗадача: «{running['task']}»"
                         + (f"\n\nВ очереди ещё сообщений: {left}." if left else ""))
        elif cmd == "stop":
            dropped = 0
            while True:
                try:
                    self.tasks.get_nowait()
                    dropped += 1
                except queue.Empty:
                    break
            if stop_codex():
                self.say("⏹ Остановил." + (f" Очередь ({dropped}) тоже очистил." if dropped else ""))
            else:
                self.say("Я сейчас ничего не делаю, останавливать нечего.")
        elif cmd == "usage":
            self.say(usage_text())
        elif cmd == "today":
            day = ARCHIVE / f"{datetime.now():%Y-%m-%d}.md"
            self.tasks.put([dict(msg, text=(
                f"/today — итоги дня. Посмотри наш разговор и архив переписки {day} (если есть). "
                "Коротко, списком: 1) что сделали сегодня; 2) что начали и не закончили; "
                "3) что ждёт моего ответа или решения."))])

    def handle(self, msg):
        text = (msg.get("text") or "").strip()
        m = re.fullmatch(r"/(\w+)(@\w+)?", text)
        if m and m[1] in ("start", "new", "now", "stop", "usage", "today"):
            self.command(m[1], msg)
            return
        gid = msg.get("media_group_id")
        if gid:
            self.groups.setdefault(gid, {"msgs": [], "at": 0})
            self.groups[gid]["msgs"].append(msg)
            self.groups[gid]["at"] = time.time()
        else:
            self.tasks.put([msg])

    def flush_groups(self):
        for gid, g in list(self.groups.items()):
            if time.time() - g["at"] > 2:
                self.tasks.put(sorted(g["msgs"], key=lambda x: x["message_id"]))
                del self.groups[gid]

    def serve(self):
        lock = (STATE / "lock").open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit("Бот уже запущен: второй копии нельзя.")
        WORK.mkdir(parents=True, exist_ok=True)
        OUTGOING.mkdir(mode=0o700, parents=True, exist_ok=True)
        INBOX.mkdir(mode=0o700, parents=True, exist_ok=True)
        for f in INBOX.iterdir():
            if time.time() - f.stat().st_mtime > 7 * 86400:
                f.unlink(missing_ok=True)
        if self.state.pop("running", None):
            self.state.setdefault("outbox", []).append(
                {"text": "Бот перезапустился во время задачи. Проверьте результат перед повтором."})
        self.commit()
        try:
            telegram(self.token, "setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except TelegramError:
            pass
        threading.Thread(target=self.worker, daemon=True).start()
        print("Telegram Codex запущен.", flush=True)
        while True:
            try:
                while self.state.get("outbox"):
                    send_reply(self.token, self.owner, self.state["outbox"][0])
                    with self.lock:
                        self.state["outbox"].pop(0)
                    self.commit()
                # После недели тишины Telegram может начать нумерацию обновлений заново.
                offset = self.state.get("offset", 0) if time.time() - self.state.get("seen", 0) < 6 * 86400 else 0
                updates = telegram(self.token, "getUpdates", offset=offset, timeout=1 if self.groups else 25,
                                   allowed_updates=["message"], request_timeout=40)
            except TelegramError as exc:
                print(str(exc), file=sys.stderr, flush=True)
                if exc.code in (401, 404, 409):
                    sys.exit("Проверьте токен и что бота не опрашивает другая программа.")
                if self.state.get("outbox") and exc.code in (400, 403):
                    with self.lock:
                        self.state["outbox"].pop(0)
                    self.commit()
                time.sleep(max(1, exc.delay))
                continue
            for update in updates:
                msg = update.get("message", {})
                chat = msg.get("chat", {})
                with self.lock:
                    self.state.update(offset=update["update_id"] + 1, seen=time.time())
                if chat.get("type") == "private" and chat.get("id") == self.owner \
                        and msg.get("from", {}).get("id") == self.owner:
                    self.handle(msg)
                self.commit()
            self.flush_groups()


if __name__ == "__main__":
    os.umask(0o077)
    for directory in (CONFIG, STATE):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sys.argv[1:] == ["setup"]:
        setup()
    elif not sys.argv[1:]:
        if not (CONFIG / "token").exists() or not (CONFIG / "owner").exists():
            sys.exit("Бот не настроен: запустите python3 telegram_codex.py setup")
        Bot((CONFIG / "token").read_text().strip(), int((CONFIG / "owner").read_text().strip())).serve()
    elif len(sys.argv) == 3 and sys.argv[1] == "send":
        try:
            send_attachment((CONFIG / "token").read_text().strip(),
                            int((CONFIG / "owner").read_text().strip()), sys.argv[2])
        except (OSError, ValueError, TelegramError) as exc:
            sys.exit(str(exc))
    else:
        sys.exit(__doc__)
