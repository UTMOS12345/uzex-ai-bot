"""Telegram bot (aiogram 3, long polling) in front of agent.answer()."""
import asyncio
import html
import re
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

import agent

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
for noisy in ("httpx", "httpx2", "openai", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("bot")

MAX_LEN = 4096
MAX_PARALLEL = int(os.getenv("BOT_MAX_PARALLEL", "8"))  # questions answered at the same time
_slots = asyncio.Semaphore(MAX_PARALLEL)

WAIT_TEXT = "⏳ Hisoblanmoqda… / Считаю…"
ERROR_TEXT = "Texnik xatolik, qayta yuboring / Техническая ошибка, повторите"
START_TEXT = (
    "Assalomu alaykum! Men UZEX birja bitimlari reyestri bo‘yicha savollarga <b>faqat reyestr ma’lumotlari asosida</b> "
    "javob beraman (2022–2026). Savolni o‘zbek yoki rus tilida yozing.\n\n"
    "Здравствуйте! Я отвечаю на вопросы о биржевых сделках UZEX <b>только по данным реестра</b> (2022–2026). "
    "Пишите на узбекском или русском.\n\n"
    "Masalan / Например:\n"
    "• 2025-yilda paxta bo‘yicha nechta bitim tuzilgan?\n"
    "• 2023-yilda savdo summasi bo‘yicha eng yirik sotuvchi kim?\n"
    "• Какая общая сумма исполненных сделок по цементу в 2024 году?\n"
    "• Кто покупатель по сделке №250000 и какова её сумма?"
)

dp = Dispatcher()


def _cell(v, max_len: int = 32) -> str:
    s = agent.fmt_value(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


def _kv(pairs: list[tuple[str, str]]) -> str:
    """Two aligned columns: key  value."""
    w = min(max((len(k) for k, _ in pairs), default=0), 16)
    return "\n".join(f"{k.ljust(w)}  {v}" if k else v for k, v in pairs)


def _result_table(ev: dict, n_rows: int) -> str:
    labels = ev.get("labels") or {}
    heads = [labels.get(c, c) for c in ev["columns"]]
    rows = ev["rows"][:n_rows]
    if len(rows) == 1:  # one row: vertical "label  value" — easiest to read on a phone
        return _kv([(h, _cell(v)) for h, v in zip(heads, rows[0])])
    cells = [[_cell(v, 24) for v in r] for r in rows]
    widths = [max(len(h[:24]), *(len(r[i]) for r in cells)) for i, h in enumerate(heads)]
    line = lambda vals: " │ ".join(str(v).ljust(w) for v, w in zip(vals, widths)).rstrip()
    return "\n".join([line([h[:24] for h in heads]), "─┼─".join("─" * w for w in widths)] + [line(r) for r in cells])


def _basis_pairs(basis: str) -> list[tuple[str, str]]:
    """'Manba: reyestr · Davr: 2025' -> [('Manba', 'reyestr'), ('Davr', '2025')]"""
    pairs = []
    for part in (p.strip() for p in basis.split("·")):
        if not part:
            continue
        k, sep, v = part.partition(":")
        k = k.strip()
        pairs.append((k[:1].upper() + k[1:], v.strip()) if sep and len(k) <= 20 else ("", part))
    return pairs


_SQL_KW = re.compile(r"\b(?:FROM|WHERE|AND|OR|GROUP BY|ORDER BY|LIMIT|HAVING|UNION|(?:LEFT |INNER |RIGHT |FULL |CROSS )?JOIN)\b", re.I)


def _pretty_sql(sql: str) -> str:
    """One clause per line, breaking only outside parentheses (keeps EXTRACT(YEAR FROM x) intact)."""
    sql = " ".join(sql.split())
    out, last = [], 0
    for m in _SQL_KW.finditer(sql):
        before = sql[:m.start()]
        if before.count("(") == before.count(")") and m.start() > 0 and sql[m.start() - 1] == " ":
            out.append(sql[last:m.start() - 1])
            last = m.start()
    out.append(sql[last:])
    return "\n".join(out)


_FAMILY = re.compile(r"product_family\s*(=\s*'[^']+'|IN\s*\([^)]*\))", re.I)


def _variants_note(sql: str, lang: str) -> str:
    """'Хлопок-сырец: 65 spellings merged' for every product family the query filters on."""
    fams = [f for m in _FAMILY.findall(sql) for f in re.findall(r"'([^']+)'", m)]
    notes = []
    for fam in dict.fromkeys(fams):
        n = agent.family_variants().get(fam, 0)
        if n > 1:
            notes.append(f"{fam}: {n} xil yozilish birlashtirildi" if lang == "uz"
                         else f"{fam}: объединено {n} вариантов написания")
    return "; ".join(notes)


def render(r: dict) -> str:
    """Answer in bold; readable basis + result table; SQL collapsed; time. Always <= 4096 chars."""
    lang = r.get("lang", "ru")
    answer = f"<b>{html.escape(r['text'])}</b>"
    steps = [e for e in r.get("evidence", []) if not e.get("error")]
    footer = f"⏱ {r['seconds']:.1f} s" + (f" · SQL: {len(steps)}" if steps else "")
    if not steps:
        return f"{answer}\n\n{footer}"
    ev = steps[-1]  # the query the answer is based on
    pairs = _basis_pairs(ev.get("basis", ""))
    note = _variants_note(ev["sql"], lang)
    if note:
        pairs.append(("Variant" if lang == "uz" else "Варианты", note))
    sql = _pretty_sql(ev["sql"].strip())
    for n_rows in (5, 3, 1):
        for sql_limit in (None, 1500, 600, 200):
            s = sql if sql_limit is None or len(sql) <= sql_limit else sql[:sql_limit] + " …"
            block = _kv(pairs)
            if ev["row_count"]:
                block += ("\n" if block else "") + "─" * 22 + "\n" + _result_table(ev, n_rows)
            parts = [answer, "", "📊 <b>Asos / Основание</b>", f"<pre>{html.escape(block)}</pre>"]
            if ev["row_count"] > n_rows:
                parts.append(f"<i>… {ev['row_count']} qator / строк</i>")
            parts.append(f"<blockquote expandable>🔎 SQL\n<code>{html.escape(s)}</code></blockquote>")
            parts.append(footer)
            text = "\n".join(parts)
            if len(text) <= MAX_LEN:
                return text
    return f"{answer}\n\n{footer}"[:MAX_LEN]


def _plain(text: str) -> str:
    """HTML -> plain text, used if Telegram rejects the formatted message."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))[:MAX_LEN]


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(START_TEXT)


@dp.message(F.text)
async def on_question(message: Message) -> None:
    status_msg = None
    try:
        status_msg = await message.answer(WAIT_TEXT)
        async with _slots:
            r = await asyncio.to_thread(agent.answer, message.text)
        log.info("user=%s status=%s provider=%s seconds=%s q=%r", message.from_user and message.from_user.id,
                 r["status"], r["provider"], r["seconds"], message.text[:200])
        text = render(r)
    except Exception:  # noqa: BLE001 - never crash the bot
        log.exception("failed to answer %r", message.text)
        text = ERROR_TEXT
    # 1) edit the "⏳" message  2) new message  3) same text without formatting (if Telegram rejects the HTML)
    attempts = ([lambda: status_msg.edit_text(text)] if status_msg else []) + [
        lambda: message.answer(text),
        lambda: message.answer(_plain(text), parse_mode=None),
    ]
    for attempt in attempts:
        try:
            await attempt()
            return
        except Exception:  # noqa: BLE001
            log.exception("sending the answer failed, trying the next way")


@dp.message()
async def on_other(message: Message) -> None:
    await message.answer("Savolni matn ko‘rinishida yuboring / Отправьте вопрос текстом")


async def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is not set")
    agent.system_prompt()  # warm up: open the DB and build the prompt before the first question
    log.info("providers: %s", [p.name for p in agent.PROVIDERS])
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)  # ignore questions sent while the bot was down
    await dp.start_polling(bot, handle_as_tasks=True)  # each update in its own task -> users in parallel


if __name__ == "__main__":
    asyncio.run(main())
