#!/usr/bin/env python3
"""Доработки официального Telegram-плагина Claude Code (telegram@claude-plugins-official).

Вносит правки в установленную копию плагина на этом компьютере:
  new        — команда /new: новый диалог с чистого листа и подтверждение после перезапуска
  archive    — архив переписки: каждое сообщение и ответ дописываются в файл дня
  forwards   — пересылки показывают, откуда они; опросы, истории и новые типы сообщений не теряются
  document   — ответ может отправить фото файлом, без сжатия Telegram
  commands   — команды /now, /stop, /usage, /today для владельца (выполняет agent.py)
  restart    — «✅ Перезапустился» после перезапуска с тем же разговором (agent.py restart)
  typing     — «печатает…» держится всё время, пока бот работает, а не 5 секунд
               (конец работы отмечает Stop-хук из templates/tg-agent.settings.json)

Запуск:  python3 patch.py            внести правки (повторный запуск безопасен)
         python3 patch.py --check    только показать, какие правки стоят
         python3 patch.py --plugin-dir DIR   работать с другой папкой плагина (для проверки)

Обновление плагина стирает правки — просто запустите скрипт ещё раз.
Если правка не подходит к новой версии плагина, скрипт её пропускает и говорит об этом:
плагин продолжает работать как официальный. Перед первой правкой сохраняется server.ts.orig.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TAG = "claude-telegram-agent"

NEW_COMMAND = """// TAG: new — команда /new. Пишет флаг new-session.flag и перезапускает службу бота
// (имя службы — в TELEGRAM_SERVICE). Скрипт запуска, увидев флаг, начинает диалог заново,
// а после старта здесь же приходит подтверждение.
const NEW_FLAG = join(STATE_DIR, 'new-session.flag')

bot.command('new', async ctx => {
  const gated = dmCommandGate(ctx)
  if (!gated || !gated.access.allowFrom.includes(gated.senderId)) return
  const service = process.env.TELEGRAM_SERVICE
  if (!service) {
    await ctx.reply('Команда /new не настроена: не указана служба бота (TELEGRAM_SERVICE).').catch(() => {})
    return
  }
  writeFileSync(NEW_FLAG, JSON.stringify({ chat_id: String(ctx.chat!.id), at: Date.now() }), { mode: 0o600 })
  await ctx.reply('Начинаю новый диалог. Через полминуты напишу, что готово.').catch(() => {})
  try {
    execFileSync('systemd-run', ['--user', '--collect', '--quiet', '--on-active=2',
      'systemctl', '--user', 'restart', service], { stdio: 'ignore', timeout: 15000 })
  } catch (err) {
    rmSync(NEW_FLAG, { force: true })
    process.stderr.write(`telegram channel: /new failed: ${err}\\n`)
    await ctx.reply('Не получилось начать новый диалог. Попросите того, кто настраивал бота, проверить службу.').catch(() => {})
  }
})

""".replace("TAG", TAG)

NEW_CONFIRM = """            { scope: { type: 'all_private_chats' } },
          ).catch(() => {})
          // TAG: new — подтверждение после перезапуска по /new
          try {
            const f = JSON.parse(readFileSync(NEW_FLAG, 'utf8'))
            rmSync(NEW_FLAG, { force: true })
            if (f.chat_id) void bot.api.sendMessage(f.chat_id, '✅ Новый диалог начат. Пишите.').catch(() => {})
          } catch {}
""".replace("TAG", TAG)

COMMANDS = """// TAG: commands — команды владельца. /now, /stop, /usage выполняет agent.py из набора,
// до Claude они не доходят и работают, даже когда он занят. /today уходит Claude готовой просьбой.
const AGENT_PY = process.env.TELEGRAM_AGENT_TOOL ?? join(homedir(), '.local/share/telegram-agent/agent.py')

function ownerOnly(ctx: Context): boolean {
  const gated = dmCommandGate(ctx)
  return !!gated && gated.access.allowFrom.includes(gated.senderId)
}

function runAgent(cmd: string, timeoutMs: number): Promise<string> {
  return new Promise(resolve => {
    execFile('python3', [AGENT_PY, cmd], { timeout: timeoutMs, encoding: 'utf8' }, (err, stdout) => {
      if (err && !stdout) process.stderr.write(`telegram channel: agent.py ${cmd} failed: ${err}\\n`)
      resolve((stdout ?? '').trim())
    })
  })
}

for (const [cmd, timeout] of [['now', 15000], ['stop', 15000], ['usage', 60000]] as const) {
  bot.command(cmd, async ctx => {
    if (!ownerOnly(ctx)) return
    if (cmd === 'usage') void bot.api.sendChatAction(String(ctx.chat!.id), 'typing').catch(() => {})
    const out = await runAgent(cmd, timeout)
    await ctx.reply(out || 'Не получилось, попробуйте ещё раз через минуту.').catch(() => {})
  })
}

bot.command('today', async ctx => {
  if (!ownerOnly(ctx)) return
  const d = new Date()
  const p2 = (n: number) => String(n).padStart(2, '0')
  const day = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`
  const archive = process.env.TELEGRAM_ARCHIVE_DIR ? ` и архив переписки ${join(process.env.TELEGRAM_ARCHIVE_DIR, day + '.md')}` : ''
  await handleInbound(ctx,
    `/today — итоги дня. Посмотри текущий разговор${archive}. Коротко, списком: ` +
    `1) что сделали сегодня; 2) что начали и не закончили; 3) что ждёт моего ответа или решения.`, undefined)
})

""".replace("TAG", TAG)

RESTART_CONFIRM = """            { scope: { type: 'all_private_chats' } },
          ).catch(() => {})
          // TAG: restart — подтверждение после перезапуска с тем же разговором (agent.py restart)
          try {
            const rf = join(STATE_DIR, 'restart.flag')
            const f = JSON.parse(readFileSync(rf, 'utf8'))
            rmSync(rf, { force: true })
            if (f.chat_id) void bot.api.sendMessage(f.chat_id, '✅ Перезапустился, разговор тот же.').catch(() => {})
          } catch {}
""".replace("TAG", TAG)

ARCHIVE_HELPER = """const INBOX_DIR = join(STATE_DIR, 'inbox')

// TAG: archive — архив переписки. Если задан TELEGRAM_ARCHIVE_DIR, каждое входящее
// сообщение и каждый ответ дописываются в <папка>/ГГГГ-ММ-ДД.md (местное время).
const ARCHIVE_DIR = process.env.TELEGRAM_ARCHIVE_DIR
function archiveNote(who: string, text: string): void {
  if (!ARCHIVE_DIR) return
  try {
    const d = new Date()
    const p2 = (n: number) => String(n).padStart(2, '0')
    const file = join(ARCHIVE_DIR, `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}.md`)
    mkdirSync(ARCHIVE_DIR, { recursive: true, mode: 0o700 })
    let head = ''
    try { statSync(file) } catch { head = `# Переписка ${p2(d.getDate())}.${p2(d.getMonth() + 1)}.${d.getFullYear()}\\n` }
    appendFileSync(file, `${head}\\n**${p2(d.getHours())}:${p2(d.getMinutes())} · ${who}**\\n${text}\\n`, { mode: 0o600 })
  } catch (err) {
    process.stderr.write(`telegram channel: archive failed: ${err}\\n`)
  }
}
""".replace("TAG", TAG)

ARCHIVE_IN = """  // TAG: archive — входящее сообщение
  {
    const fo: any = (ctx.message as any)?.forward_origin
    const extra: string[] = []
    if (fo) extra.push('переслано от ' + (fo.chat?.title ?? fo.sender_user?.first_name ?? fo.sender_user_name ?? fo.sender_chat?.title ?? '?'))
    if (imagePath) extra.push(`фото ${imagePath}`)
    if (attachment) extra.push(attachment.kind + (attachment.name ? `: ${attachment.name}` : ''))
    archiveNote(from.first_name ?? from.username ?? String(from.id), (extra.length ? `[${extra.join('; ')}]\\n` : '') + text)
  }

  // image_path goes in meta only""".replace("TAG", TAG)

ARCHIVE_OUT = """        // TAG: archive — ответ бота
        archiveNote('бот', text + (files.length ? `\\n[файлы: ${files.join(', ')}]` : ''))

        const result =""".replace("TAG", TAG)

FORWARDS_HANDLER = """// TAG: forwards — сообщения типов, которые плагин молча выбрасывал (опросы, платные посты,
// истории, геоточки, новые типы вроде rich_message), доходят с кратким описанием.
bot.on('message', async ctx => {
  const m: any = ctx.message
  const keys = Object.keys(m).filter(k => !['message_id', 'from', 'chat', 'date', 'forward_origin',
    'forward_from', 'forward_from_chat', 'forward_from_message_id', 'forward_signature',
    'forward_sender_name', 'forward_date', 'is_automatic_forward', 'entities', 'caption_entities',
    'has_protected_content', 'media_group_id', 'author_signature', 'edit_date', 'link_preview_options',
    'reply_to_message', 'via_bot', 'sender_chat', 'reply_markup'].includes(k))
  process.stderr.write(`telegram channel: unhandled message type, keys=${keys.join(',')}\\n`)
  const parts: string[] = []
  if (m.caption) parts.push(m.caption)
  if (m.text) parts.push(m.text)
  if (m.poll) {
    parts.push(`(poll) ${m.poll.question}`)
    for (const o of m.poll.options ?? []) parts.push(`- ${o.text} (${o.voter_count ?? 0})`)
  }
  if (m.paid_media) parts.push(`(paid media: ${m.paid_media.paid_media?.length ?? '?'} items, ${m.paid_media.star_count ?? '?'} stars)`)
  if (m.story) parts.push(`(forwarded story: ${safeName(m.story.chat?.username ?? m.story.chat?.title) ?? '?'} #${m.story.id})`)
  if (m.location) parts.push(`(location ${m.location.latitude}, ${m.location.longitude})`)
  if (m.venue) parts.push(`(venue ${m.venue.title}, ${m.venue.address})`)
  if (m.contact) parts.push(`(contact ${m.contact.first_name ?? ''} ${m.contact.last_name ?? ''})`)
  if (m.dice) parts.push(`(dice ${m.dice.emoji} ${m.dice.value})`)
  // Новые и неизвестные типы: сохранить сырой JSON и вытащить весь читаемый текст.
  const knownKeys = ['text', 'caption', 'poll', 'paid_media', 'story', 'location', 'venue', 'contact', 'dice', 'animation', 'is_from_offline']
  const unknown = keys.filter(k => !knownKeys.includes(k))
  if (unknown.length > 0) {
    const strs: string[] = []
    const walk = (v: any, depth: number) => {
      if (depth > 12 || strs.length > 2000) return
      if (typeof v === 'string') { if (v.trim() && !strs.includes(v)) strs.push(v); return }
      if (Array.isArray(v)) { for (const x of v) walk(x, depth + 1); return }
      if (v && typeof v === 'object') {
        for (const [k, x] of Object.entries(v)) {
          if (k === 'file_id' || k === 'file_unique_id' || k === 'type') continue
          walk(x, depth + 1)
        }
      }
    }
    for (const k of unknown) walk(m[k], 0)
    try {
      mkdirSync(INBOX_DIR, { recursive: true })
      const rawPath = join(INBOX_DIR, `${Date.now()}-${m.message_id}-raw.json`)
      writeFileSync(rawPath, JSON.stringify(m, null, 2))
      parts.push(`(${unknown.join(', ')}; raw json saved to inbox as ${rawPath.split('/').pop()})`)
    } catch (err) {
      process.stderr.write(`telegram channel: raw dump failed: ${err}\\n`)
      parts.push(`(${unknown.join(', ')})`)
    }
    if (strs.length) parts.push(strs.join('\\n'))
  }
  if (parts.length === 0) parts.push(`(unsupported message: ${keys.join(', ') || 'unknown'})`)
  const anim = m.animation
  await handleInbound(ctx, parts.join('\\n'), undefined, anim ? {
    kind: 'animation', file_id: anim.file_id, size: anim.file_size, mime: anim.mime_type, name: safeName(anim.file_name),
  } : undefined)
})

// Откуда переслано: имя и, для публичных каналов, ссылка на пост.
function forwardMeta(ctx: Context): Record<string, string> {
  const o: any = (ctx.message as any)?.forward_origin
  if (!o) return {}
  const out: Record<string, string> = {}
  if (o.type === 'channel') {
    const name = o.chat?.username ? `@${o.chat.username}` : o.chat?.title
    if (name) out.forwarded_from = safeName(name)!
    if (o.chat?.username && o.message_id) out.forward_link = `https://t.me/${o.chat.username}/${o.message_id}`
  } else if (o.type === 'user') {
    const u = o.sender_user
    out.forwarded_from = safeName(u?.username ? `@${u.username}` : [u?.first_name, u?.last_name].filter(Boolean).join(' ')) ?? 'user'
  } else if (o.type === 'hidden_user') {
    out.forwarded_from = safeName(o.sender_user_name) ?? 'hidden user'
  } else if (o.type === 'chat') {
    out.forwarded_from = safeName(o.sender_chat?.username ? `@${o.sender_chat.username}` : o.sender_chat?.title) ?? 'chat'
  }
  return out
}

type AttachmentMeta = {""".replace("TAG", TAG)

TYPING_HELPER = """// TAG: typing — «печатает…» обновляется каждые 4 с, пока Claude работает над сообщением.
// Конец хода отмечает Stop-хук в настройках бота: touch $TELEGRAM_STATE_DIR/turn-done.
// Без хука индикатор гаснет сам через 30 минут.
const TURN_DONE_FILE = join(STATE_DIR, 'turn-done')
const TYPING_LIMIT_MS = 30 * 60_000
const typingLoops = new Map<string, ReturnType<typeof setInterval>>()
function turnDoneAt(): number {
  try { return statSync(TURN_DONE_FILE).mtimeMs } catch { return 0 }
}
function startTyping(chat_id: string): void {
  const started = Date.now()
  const prev = typingLoops.get(chat_id)
  if (prev) clearInterval(prev)
  const timer = setInterval(() => {
    if (turnDoneAt() > started || Date.now() - started > TYPING_LIMIT_MS) {
      clearInterval(timer)
      if (typingLoops.get(chat_id) === timer) typingLoops.delete(chat_id)
      return
    }
    void bot.api.sendChatAction(chat_id, 'typing').catch(() => {})
  }, 4000)
  typingLoops.set(chat_id, timer)
  void bot.api.sendChatAction(chat_id, 'typing').catch(() => {})
}

async function handleInbound(
""".replace("TAG", TAG)

# Правка = (название, что делает, [(якорь в исходнике, чем заменить)]).
# Якорь должен встречаться ровно один раз; иначе правка пропускается целиком.
PATCHES = [
    ("new", "команда /new и подтверждение", [
        ("// Inline-button handler for permission requests.",
         NEW_COMMAND + "// Inline-button handler for permission requests."),
        ("              { command: 'status', description: 'Check your pairing status' },\n",
         "              { command: 'status', description: 'Check your pairing status' },\n"
         "              { command: 'new', description: 'Начать новый диалог' },\n"),
        ("            { scope: { type: 'all_private_chats' } },\n          ).catch(() => {})\n", NEW_CONFIRM),
    ]),
    ("archive", "архив переписки", [
        ("import { readFileSync, writeFileSync,", "import { appendFileSync, readFileSync, writeFileSync,"),
        ("const INBOX_DIR = join(STATE_DIR, 'inbox')\n", ARCHIVE_HELPER),
        ("  // image_path goes in meta only", ARCHIVE_IN),
        ("        const result =", ARCHIVE_OUT),
    ]),
    ("forwards", "пересылки и все типы сообщений", [
        ("type AttachmentMeta = {", FORWARDS_HANDLER),
        ("        ...(imagePath ? { image_path: imagePath } : {}),\n",
         "        ...(imagePath ? { image_path: imagePath } : {}),\n"
         f"        ...forwardMeta(ctx), // {TAG}: forwards\n"),
    ]),
    ("document", "фото файлом без сжатия", [
        ("            description: 'Absolute file paths to attach. Images send as photos (inline preview); other types as documents. Max 50MB each.',\n          },\n",
         "            description: 'Absolute file paths to attach. Images send as photos (inline preview); other types as documents. Max 50MB each.',\n          },\n"
         f"          // {TAG}: document\n"
         "          as_document: {\n"
         "            type: 'boolean',\n"
         "            description: 'Send every file as a document, without Telegram compression. Use for photos where quality matters.',\n"
         "          },\n"),
        ("        const files = (args.files as string[] | undefined) ?? []\n",
         "        const files = (args.files as string[] | undefined) ?? []\n"
         "        const asDocument = args.as_document === true\n"),
        ("          if (PHOTO_EXTS.has(ext)) {\n            const sent = await bot.api.sendPhoto",
         "          if (PHOTO_EXTS.has(ext) && !asDocument) {\n            const sent = await bot.api.sendPhoto"),
    ]),
    ("commands", "команды /now, /stop, /usage, /today", [
        ("import { execFileSync } from 'child_process'", "import { execFile, execFileSync } from 'child_process'"),
        ("// Inline-button handler for permission requests.",
         COMMANDS + "// Inline-button handler for permission requests."),
        ("              { command: 'status', description: 'Check your pairing status' },\n",
         "              { command: 'status', description: 'Check your pairing status' },\n"
         "              { command: 'now', description: 'Что я сейчас делаю' },\n"
         "              { command: 'stop', description: 'Остановить текущую работу' },\n"
         "              { command: 'today', description: 'Итоги дня' },\n"
         "              { command: 'usage', description: 'Расход лимитов Claude' },\n"),
    ]),
    ("restart", "подтверждение после перезапуска", [
        ("            { scope: { type: 'all_private_chats' } },\n          ).catch(() => {})\n", RESTART_CONFIRM),
    ]),
    ("typing", "«печатает…», пока бот работает", [
        # Сначала замена вызова: в TYPING_HELPER та же строка, её трогать нельзя.
        ("  void bot.api.sendChatAction(chat_id, 'typing').catch(() => {})\n",
         f"  startTyping(chat_id) // {TAG}: typing\n"),
        ("async function handleInbound(\n", TYPING_HELPER),
    ]),
]


def plugin_dirs():
    base = Path.home() / ".claude/plugins/cache/claude-plugins-official/telegram"
    return sorted(p for p in base.glob("*") if (p / "server.ts").exists())


def find_bun():
    for b in (shutil.which("bun"), str(Path.home() / ".bun/bin/bun")):
        if b and Path(b).exists():
            return b
    return None


def compiles(src):
    """Проверить, что исправленный server.ts собирается (bun build)."""
    bun = find_bun()
    if not bun:
        return True, "bun не найден, сборку не проверял"
    with tempfile.TemporaryDirectory() as out:
        r = subprocess.run([bun, "build", str(src), "--target=bun", "--outdir", out],
                           capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or r.stdout)[-400:]


def apply(text):
    report = []
    for name, title, edits in PATCHES:
        if f"{TAG}: {name}" in text:
            report.append((name, title, "уже стоит"))
            continue
        if any(text.count(anchor) != 1 for anchor, _ in edits):
            report.append((name, title, "НЕ ПОДОШЛА к этой версии плагина, пропущена"))
            continue
        for anchor, new in edits:
            text = text.replace(anchor, new)
        report.append((name, title, "поставлена"))
    return text, report


STOP_HOOK = 'touch "${TELEGRAM_STATE_DIR:-$HOME/.claude/channels/telegram}/turn-done"'


def ensure_stop_hook():
    """Правке typing нужен Stop-хук в настройках бота. Дописать, если его там нет."""
    f = Path.home() / ".claude/tg-agent.settings.json"
    if not f.exists():
        return
    d = json.loads(f.read_text())
    stop = d.setdefault("hooks", {}).setdefault("Stop", [])
    if any(h.get("command") == STOP_HOOK for e in stop for h in e.get("hooks", [])):
        return
    stop.append({"matcher": "", "hooks": [{"type": "command", "command": STOP_HOOK}]})
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    print(f"В {f} добавлен Stop-хук: «печатает…» гаснет, когда бот закончил.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--plugin-dir")
    args = ap.parse_args()
    dirs = [Path(args.plugin_dir)] if args.plugin_dir else plugin_dirs()
    if not dirs:
        sys.exit("Плагин telegram@claude-plugins-official не установлен. Сначала: claude plugin install telegram@claude-plugins-official")
    failed = False
    if not args.check and not args.plugin_dir:
        ensure_stop_hook()
    for d in dirs:
        server = d / "server.ts"
        old = server.read_text()
        new, report = apply(old)
        print(f"Плагин {d.name} ({d}):")
        for name, title, status in report:
            print(f"  {title}: {status}")
            failed |= status.startswith("НЕ")
        if args.check or new == old:
            continue
        orig = d / "server.ts.orig"
        if not orig.exists():
            shutil.copy2(server, orig)
        tmp = d / "server.ts.new"
        tmp.write_text(new)
        ok, msg = compiles(tmp)
        if not ok:
            tmp.unlink()
            print(f"  ОШИБКА: исправленный плагин не собирается, правки не внесены.\n  {msg}")
            failed = True
            continue
        os.replace(tmp, server)
        print("  Сохранено. Чтобы правки заработали, перезапустите бота.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
