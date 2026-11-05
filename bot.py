"""Telegram bot (aiogram 3, long polling) in front of agent.answer(). Formatting/UX only — no agent logic here."""
import asyncio
import html
import logging
import os
import re
from collections import OrderedDict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)
from dotenv import load_dotenv

import agent

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
for noisy in ("httpx", "httpx2", "openai", "anthropic"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("bot")

MAX_LEN = 4096
MAX_QUESTION = 500                                       # longer input is rejected politely
MAX_PARALLEL = int(os.getenv("BOT_MAX_PARALLEL", "8"))   # questions answered at the same time
_slots = asyncio.Semaphore(MAX_PARALLEL)

# ------------------------------------------------------------------ texts

WELCOME = (
    "Assalomu alaykum! Men UZEX birja bitimlari reyestri bo‘yicha savollarga <b>faqat reyestr ma’lumotlari asosida</b> "
    "javob beraman (2022–2026). Savolni o‘zbek yoki rus tilida yozing.\n\n"
    "Здравствуйте! Я отвечаю на вопросы о биржевых сделках UZEX <b>только по данным реестра</b> (2022–2026). "
    "Пишите на узбекском или русском."
)
EXAMPLES_TITLE = "👇 Misollar / Примеры:"
EXAMPLES = [
    "2025-yilda paxta bo‘yicha nechta bitim tuzilgan?",
    "2023-yilda savdo summasi bo‘yicha eng yirik sotuvchi kim?",
    "Какая общая сумма исполненных сделок по цементу в 2024 году?",
    "Кто покупатель по сделке №250000 и какова её сумма?",
]
BTN_HELP = "❓ Yordam / Помощь"
BTN_EXAMPLES = "💡 Misollar / Примеры"

# per-language labels: 'uz' (Latin), 'uz_cyr' (Uzbek Cyrillic), 'ru'
T = {
    "wait":     {"uz": "⏳ Qidirilmoqda…", "uz_cyr": "⏳ Қидирилмоқда…", "ru": "⏳ Ищу в реестре…"},
    "basis":    {"uz": "📊 Asos", "uz_cyr": "📊 Асос", "ru": "📊 Основание"},
    "rows":     {"uz": "qator", "uz_cyr": "қатор", "ru": "строк"},
    "retry":    {"uz": "🔄 Qayta", "uz_cyr": "🔄 Қайта", "ru": "🔄 Повторить"},
    "variants": {"uz": "{fam}: {n} xil yozilish birlashtirildi", "uz_cyr": "{fam}: {n} хил ёзилиш бирлаштирилди",
                 "ru": "{fam}: объединено {n} вариантов написания"},
    "variant_key": {"uz": "Variant", "uz_cyr": "Вариант", "ru": "Варианты"},
    "error":    {"uz": "Kechirasiz, texnik xatolik yuz berdi. Savolni qayta yuboring yoki 🔄 tugmasini bosing.",
                 "uz_cyr": "Кечирасиз, техник хатолик юз берди. Саволни қайта юборинг ёки 🔄 тугмасини босинг.",
                 "ru": "Извините, произошла техническая ошибка. Отправьте вопрос ещё раз или нажмите 🔄."},
    "about":    {"uz": "Men UZEX birja bitimlari bo‘yicha savollarga faqat reyestr ma’lumotlari asosida javob beraman. "
                       "Masalan:",
                 "uz_cyr": "Мен UZEX биржа битимлари бўйича саволларга фақат реестр маълумотлари асосида жавоб "
                           "бераман. Масалан:",
                 "ru": "Я отвечаю на вопросы о биржевых сделках UZEX только по данным реестра. Например:"},
    "too_long": {"uz": "Savol juda uzun (500 belgigacha yozing).", "uz_cyr": "Савол жуда узун (500 белгигача ёзинг).",
                 "ru": "Слишком длинный вопрос (до 500 символов)."},
    "expired":  {"uz": "Bu xabar eskirgan, savolni qayta yuboring.", "uz_cyr": "Бу хабар эскирган, саволни қайта юборинг.",
                 "ru": "Сообщение устарело, отправьте вопрос заново."},
}
_GREETING = re.compile(r"^\W*(salom|assalom\w*|ассалом\w*|салом|привет\w*|здравствуй\w*|добр\w+ (день|утро|вечер)|"
                       r"hello|hi|hey|xayrli \w+)\W*$", re.I)


def lang_of(text: str) -> str:
    return agent.detect_lang(text or "")  # 'ru' | 'uz' | 'uz_cyr', no LLM involved


# ------------------------------------------------------------------ keyboards

def reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_EXAMPLES)]],
                               resize_keyboard=True, is_persistent=True)


def examples_kb(idx: list[int] | None = None) -> InlineKeyboardMarkup:
    idx = list(range(len(EXAMPLES))) if idx is None else idx
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=EXAMPLES[i], callback_data=f"ex:{i}")]
                                                 for i in idx])


def two_examples(lang: str) -> list[int]:
    return [2, 3] if lang == "ru" else [0, 1]


def answer_kb(lang: str, has_sql: bool) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=T["retry"][lang], callback_data="rerun")]
    if has_sql:
        row.append(InlineKeyboardButton(text="🔍 SQL", callback_data="sql"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


# answer message -> (question, [sql, ...]) for the 🔄 / 🔍 buttons; bounded so memory never grows
_HISTORY: OrderedDict[tuple[int, int], tuple[str, list[str]]] = OrderedDict()


def remember(chat_id: int, message_id: int, question: str, sqls: list[str]) -> None:
    _HISTORY[(chat_id, message_id)] = (question, sqls)
    while len(_HISTORY) > 2000:
        _HISTORY.popitem(last=False)


# ------------------------------------------------------------------ answer formatting

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
    line = lambda vals: " │ ".join(str(v).ljust(w) for v, w in zip(vals, widths)).rstrip()  # noqa: E731
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


_SQL_KW = re.compile(r"\b(?:FROM|WHERE|AND|OR|GROUP BY|ORDER BY|LIMIT|HAVING|UNION|"
                     r"(?:LEFT |INNER |RIGHT |FULL |CROSS )?JOIN)\b", re.I)


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
            notes.append(T["variants"][lang].format(fam=fam, n=n))
    return "; ".join(notes)


_NUMBER = re.compile(r"\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


def _bold_numbers(escaped: str) -> str:
    """Make the numbers of the answer bold (years like 2025 stay plain). Input is already HTML-escaped."""
    def sub(m: re.Match) -> str:
        tok = m.group(0)
        plain = re.fullmatch(r"(19|20)\d\d", tok) or re.fullmatch(r"\d{1,2}", tok)  # years, list numbers "1)", "top-3"
        return tok if plain else f"<b>{tok}</b>"
    return _NUMBER.sub(sub, escaped)


def render(r: dict, lang: str, simple: bool = False) -> str:
    """Answer (numbers bold) → expandable 📊 basis + table → expandable 🔎 SQL → ⏱ time. Always <= 4096 chars.
    simple=True drops the blockquotes (fallback if Telegram rejects the rich version)."""
    answer = _bold_numbers(html.escape(r["text"]))
    footer = f"⏱ {r['seconds']:.1f} s"
    steps = [e for e in r.get("evidence", []) if not e.get("error")]
    if not steps:
        return f"{answer}\n\n{footer}"
    ev = steps[-1]  # the query the answer is based on
    pairs = _basis_pairs(ev.get("basis", ""))
    note = _variants_note(ev["sql"], lang)
    if note:
        pairs.append((T["variant_key"][lang], note))
    sql = _pretty_sql(ev["sql"].strip())
    q_open, q_close = ("", "") if simple else ("<blockquote expandable>", "</blockquote>")
    for n_rows in (5, 3, 1):
        for sql_limit in (None, 1500, 600, 200):
            s = sql if sql_limit is None or len(sql) <= sql_limit else sql[:sql_limit] + " …"
            block = _kv(pairs)
            if ev["row_count"]:
                block += ("\n" if block else "") + "─" * 22 + "\n" + _result_table(ev, n_rows)
            basis = f"{q_open}<b>{T['basis'][lang]}</b>\n<code>{html.escape(block)}</code>"
            if ev["row_count"] > n_rows:
                basis += f"\n<i>… {ev['row_count']} {T['rows'][lang]}</i>"
            basis += q_close
            sql_part = f"{q_open}<b>🔎 SQL</b>\n<code>{html.escape(s)}</code>{q_close}"
            text = "\n".join([answer, "", basis, sql_part, footer])
            if len(text) <= MAX_LEN:
                return text
    return f"{answer}\n\n{footer}"[:MAX_LEN]


def _plain(text: str) -> str:
    """HTML -> plain text, last-resort fallback."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))[:MAX_LEN]


# ------------------------------------------------------------------ question flow

async def _typing(bot: Bot, chat_id: int, stop: asyncio.Event) -> None:
    """'typing…' indicator every 4 s until the answer is ready."""
    while not stop.is_set():
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4)
        except asyncio.TimeoutError:
            pass


async def _deliver(bot: Bot, chat_id: int, message_id: int | None, variants: list[str],
                   kb: InlineKeyboardMarkup | None) -> int | None:
    """Edit the ⏳ message into the answer; on failure try simpler formats / a new message. Returns message id."""
    for text, mode in [(v, ParseMode.HTML) for v in variants] + [(_plain(variants[-1]), None)]:
        try:
            if message_id:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=kb,
                                            parse_mode=mode)
                return message_id
            sent = await bot.send_message(chat_id, text, reply_markup=kb, parse_mode=mode)
            return sent.message_id
        except Exception:  # noqa: BLE001
            log.exception("delivery failed (mode=%s), trying a simpler form", mode)
            message_id = None  # the edit target may be gone: next attempts send a new message
    return None


async def run_question(bot: Bot, chat_id: int, question: str, user_id: int | None) -> None:
    question = (question or "").strip()
    lang = lang_of(question)
    if not question or _GREETING.match(question):  # greeting / empty: explain + 2 examples, no LLM call
        await bot.send_message(chat_id, html.escape(T["about"][lang]), reply_markup=examples_kb(two_examples(lang)))
        return
    if len(question) > MAX_QUESTION:
        await bot.send_message(chat_id, html.escape(T["too_long"][lang]))
        return

    stop = asyncio.Event()
    typing = asyncio.create_task(_typing(bot, chat_id, stop))
    status_id = None
    try:
        status_id = (await bot.send_message(chat_id, T["wait"][lang])).message_id
        async with _slots:
            r = await asyncio.to_thread(agent.answer, question)
        log.info("user=%s status=%s provider=%s seconds=%s q=%r", user_id, r["status"], r["provider"],
                 r["seconds"], question[:200])
        sqls = [e["sql"] for e in r.get("evidence", []) if not e.get("error")]
        if r["status"] == "not_found" and not sqls:  # off-topic: say what the bot does + 2 examples
            text = html.escape(r["text"]) + "\n\n" + html.escape(T["about"][lang])
            await _deliver(bot, chat_id, status_id, [text], examples_kb(two_examples(lang)))
            return
        variants = [render(r, lang), render(r, lang, simple=True)]
        msg_id = await _deliver(bot, chat_id, status_id, variants, answer_kb(lang, bool(sqls)))
        if msg_id:
            remember(chat_id, msg_id, question, sqls)
    except Exception:  # noqa: BLE001 - never crash the bot
        log.exception("failed to answer %r", question)
        msg_id = await _deliver(bot, chat_id, status_id, [html.escape(T["error"][lang])], answer_kb(lang, False))
        if msg_id:
            remember(chat_id, msg_id, question, [])
    finally:
        stop.set()
        typing.cancel()


# ------------------------------------------------------------------ handlers

dp = Dispatcher()


@dp.message(CommandStart())
@dp.message(Command("help"))
@dp.message(F.text == BTN_HELP)
async def on_help(message: Message) -> None:
    await message.answer(WELCOME, reply_markup=reply_kb())        # also installs the persistent keyboard
    await message.answer(EXAMPLES_TITLE, reply_markup=examples_kb())


@dp.message(F.text == BTN_EXAMPLES)
async def on_examples(message: Message) -> None:
    await message.answer(EXAMPLES_TITLE, reply_markup=examples_kb())


@dp.message(F.text)
async def on_question(message: Message, bot: Bot) -> None:
    await run_question(bot, message.chat.id, message.text, message.from_user and message.from_user.id)


@dp.callback_query(F.data.startswith("ex:"))
async def on_example(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    i = int(cb.data.split(":")[1])
    if cb.message and 0 <= i < len(EXAMPLES):
        await bot.send_message(cb.message.chat.id, f"❔ {html.escape(EXAMPLES[i])}")  # show what was asked
        await run_question(bot, cb.message.chat.id, EXAMPLES[i], cb.from_user.id)


@dp.callback_query(F.data == "rerun")
async def on_rerun(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    if not cb.message:
        return
    item = _HISTORY.get((cb.message.chat.id, cb.message.message_id))
    if not item:
        await bot.send_message(cb.message.chat.id, f"{T['expired']['uz']}\n{T['expired']['ru']}")
        return
    await run_question(bot, cb.message.chat.id, item[0], cb.from_user.id)


@dp.callback_query(F.data == "sql")
async def on_sql(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    if not cb.message:
        return
    item = _HISTORY.get((cb.message.chat.id, cb.message.message_id))
    if not item or not item[1]:
        await bot.send_message(cb.message.chat.id, f"{T['expired']['uz']}\n{T['expired']['ru']}")
        return
    for i, sql in enumerate(item[1], 1):
        head = f"🔎 SQL {i}/{len(item[1])}\n" if len(item[1]) > 1 else ""
        await bot.send_message(cb.message.chat.id, f"{head}<pre>{html.escape(_pretty_sql(sql))[: MAX_LEN - 40]}</pre>")


@dp.message()
async def on_other(message: Message) -> None:  # stickers, photos, voice…
    await message.answer(html.escape(f"{T['about']['uz']}\n{T['about']['ru']}"), reply_markup=examples_kb([0, 2]))


async def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is not set")
    agent.system_prompt()     # warm up: open the DB and build the prompt before the first question
    agent.family_variants()
    log.info("providers: %s", [p.name for p in agent.PROVIDERS])
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)  # ignore questions sent while the bot was down
    await dp.start_polling(bot, handle_as_tasks=True)  # each update in its own task -> users in parallel


if __name__ == "__main__":
    asyncio.run(main())
