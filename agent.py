"""UZEX deals agent: question -> SQL (DuckDB) -> answer, grounded only in query results.

Usage:
    python agent.py "savol matni"      answer one question
    python agent.py --check-llm        ping every configured LLM provider
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field

import anthropic
import duckdb
import openai
from dotenv import load_dotenv

from products import ALIASES
from textnorm import norm

load_dotenv()
log = logging.getLogger("agent")

DB_PATH = os.getenv("UZEX_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "uzex.duckdb"))
LLM_TIMEOUT = 25.0          # seconds per LLM call
BREAKER_SECONDS = 60.0      # skip a provider this long after 429 / timeout
SERVER_ERROR_SECONDS = 15.0  # ... and this long after a 5xx
SQL_TIMEOUT = 10.0          # seconds per query
MAX_ROWS = 50
MAX_SQL_STEPS = 4
TOTAL_BUDGET = 60.0         # seconds per question
RETRY_WAIT = 4.0            # all providers down: wait this long, then retry ...
RETRY_MIN_REMAINING = 15.0  # ... as long as this much of the budget is left


# ============================================================ A. LLM LAYER

class LLMUnavailable(Exception):
    pass


@dataclass
class Provider:
    name: str
    type: str                      # "openai" | "anthropic"
    base_url: str | None
    api_key: str
    model: str
    max_tokens: int
    effort: str | None = None      # anthropic only: output_config.effort
    extra: dict = field(default_factory=dict)   # openai only: extra_body, e.g. {"reasoning_effort": "low"}
    disabled: bool = False         # bad key -> off for the whole run
    skip_until: float = 0.0        # circuit breaker
    send_temperature: bool = True  # turned off automatically if the model rejects it
    use_completion_tokens: bool = False  # OpenAI reasoning models want max_completion_tokens
    _client: object = None

    def client(self):
        if self._client is None:
            if self.type == "anthropic":
                kw = {"api_key": self.api_key, "max_retries": 0, "timeout": LLM_TIMEOUT}
                if self.base_url:
                    kw["base_url"] = self.base_url
                self._client = anthropic.Anthropic(**kw)
            else:
                self._client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url or None,
                                             max_retries=0, timeout=LLM_TIMEOUT)
        return self._client


def _json_env(name: str, value: str) -> dict:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        log.error("%s is not valid JSON (in .env wrap it in single quotes): %r -- ignored", name, value)
        return {}


def load_providers() -> list[Provider]:
    out = []
    for i in range(1, 10):
        key = os.getenv(f"LLM{i}_API_KEY", "").strip()
        if not key:
            continue
        ptype = (os.getenv(f"LLM{i}_TYPE") or "openai").strip().lower()
        model = os.getenv(f"LLM{i}_MODEL", "").strip()
        base_url = os.getenv(f"LLM{i}_BASE_URL", "").strip() or None
        # Claude models think before answering and thinking counts against max_tokens -> larger default
        # reasoning models (gpt-oss, Gemini 3.x, Claude) count hidden thinking against max_tokens:
        # 800 sometimes leaves nothing for the JSON -> empty reply. 2000 is safe for all tested models.
        default_max = "4000" if ptype == "anthropic" else "2000"
        extra = os.getenv(f"LLM{i}_EXTRA", "").strip()
        out.append(Provider(
            name=f"LLM{i}:{model}", type=ptype, base_url=base_url, api_key=key, model=model,
            max_tokens=int(os.getenv(f"LLM{i}_MAX_TOKENS", default_max)),
            effort=os.getenv(f"LLM{i}_EFFORT", "low" if ptype == "anthropic" else "").strip() or None,
            extra=_json_env(f"LLM{i}_EXTRA", extra),
            send_temperature=(ptype != "anthropic"),  # current Claude models reject temperature
        ))
    return out


PROVIDERS = load_providers()


def _classify(e: Exception) -> str:
    if isinstance(e, (openai.APITimeoutError, anthropic.APITimeoutError)):
        return "timeout"
    if isinstance(e, (openai.APIConnectionError, anthropic.APIConnectionError)):
        return "connection"
    status = getattr(e, "status_code", None)
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status is not None and status >= 500:
        return "server"
    if status == 400:
        return "bad_request"
    return f"error{status or ''}"


def _call_one(p: Provider, messages: list[dict], timeout: float) -> str:
    client = p.client().with_options(timeout=timeout)
    if p.type == "anthropic":
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        kw = dict(model=p.model, max_tokens=p.max_tokens, system=system,
                  messages=[m for m in messages if m["role"] != "system"])
        if p.effort:
            kw["output_config"] = {"effort": p.effort}
        if p.send_temperature:
            kw["temperature"] = 0
        r = client.messages.create(**kw)
        return "".join(b.text for b in r.content if b.type == "text")
    kw = dict(model=p.model, messages=messages)
    kw["max_completion_tokens" if p.use_completion_tokens else "max_tokens"] = p.max_tokens
    if p.send_temperature:
        kw["temperature"] = 0
    if p.extra:
        kw["extra_body"] = p.extra
    r = client.chat.completions.create(**kw)
    return (r.choices[0].message.content or "") if r.choices else ""


def _adapt(p: Provider, e: Exception) -> bool:
    """On a 400 caused by an unsupported parameter, drop/rename it. True = retry same provider."""
    msg = str(e).lower()
    if p.send_temperature and "temperature" in msg:
        p.send_temperature = False
        return True
    if p.type == "openai" and not p.use_completion_tokens and "max_tokens" in msg:
        p.use_completion_tokens = True
        return True
    if p.type == "anthropic" and p.effort and ("effort" in msg or "output_config" in msg):
        p.effort = None
        return True
    return False


def call_llm(messages: list[dict], timeout: float = LLM_TIMEOUT,
             providers: list[Provider] | None = None) -> tuple[str, str]:
    """Try providers in priority order, fail over immediately. Returns (text, provider_name)."""
    providers = PROVIDERS if providers is None else providers
    now = time.monotonic()
    live = [p for p in providers if not p.disabled]
    ready = [p for p in live if p.skip_until <= now]
    cooling = [p for p in live if p.skip_until > now]
    # breaker-skipped providers are only tried as a last resort, after every ready one failed
    for p in ready + cooling:
        for _attempt in range(2):
            t0 = time.monotonic()
            try:
                text = _call_one(p, messages, timeout)
            except Exception as e:  # noqa: BLE001 - every failure means "try the next provider"
                kind = _classify(e)
                log.warning("llm provider=%s latency=%.2fs error=%s %s", p.name, time.monotonic() - t0,
                            kind, str(e)[:200])
                if kind == "bad_request" and _adapt(p, e):
                    continue
                if kind == "auth":
                    p.disabled = True
                elif kind in ("rate_limit", "timeout"):
                    p.skip_until = time.monotonic() + BREAKER_SECONDS
                elif kind == "server":  # 503 "overloaded" usually lasts a few seconds
                    p.skip_until = time.monotonic() + SERVER_ERROR_SECONDS
                break
            latency = time.monotonic() - t0
            if not text.strip():
                log.warning("llm provider=%s latency=%.2fs error=empty_reply", p.name, latency)
                break
            log.info("llm provider=%s latency=%.2fs ok", p.name, latency)
            return text, p.name
    raise LLMUnavailable("all LLM providers failed")


def check_llm() -> None:
    if not PROVIDERS:
        print("No providers configured (set LLM1_API_KEY, LLM1_MODEL, ...).")
        return
    for p in PROVIDERS:
        t0 = time.monotonic()
        try:
            text = _call_one(p, [{"role": "user", "content": "Reply with OK"}], LLM_TIMEOUT)
            res = "OK" if text.strip() else "error: empty reply"
            if text.strip():
                res += f" ({text.strip()[:30]!r})"
        except Exception as e:  # noqa: BLE001
            if _classify(e) == "bad_request" and _adapt(p, e):
                try:
                    text = _call_one(p, [{"role": "user", "content": "Reply with OK"}], LLM_TIMEOUT)
                    res = f"OK after param fix ({text.strip()[:30]!r})"
                except Exception as e2:  # noqa: BLE001
                    res = f"error: {_classify(e2)} {str(e2)[:150]}"
            else:
                res = f"error: {_classify(e)} {str(e)[:150]}"
        print(f"{p.name:40s} type={p.type:9s} {res}  [{time.monotonic() - t0:.2f}s]")


# ============================================================ B. DATA LAYER

_DB: duckdb.DuckDBPyConnection | None = None
_DB_LOCK = threading.Lock()


def connect() -> duckdb.DuckDBPyConnection:
    """A new connection per question: a cursor on one shared read-only database (thread safe)."""
    global _DB
    with _DB_LOCK:
        if _DB is None:
            db = duckdb.connect(DB_PATH, read_only=True)
            db.create_function("norm", norm, ["VARCHAR"], "VARCHAR", null_handling="special")
            db.execute("SET enable_external_access = false")  # no read_csv('/etc/...') from generated SQL
            # shared VPS: keep RAM and CPU modest (500k rows need far less than DuckDB's default 80% of RAM)
            db.execute(f"SET memory_limit = '{os.getenv('DUCKDB_MEMORY', '256MB')}'")
            db.execute(f"SET threads = {int(os.getenv('DUCKDB_THREADS', '2'))}")
            db.execute("SET lock_configuration = true")
            _DB = db
        return _DB.cursor()


_SQL_START = re.compile(r"^\s*(select|with)\b", re.I)


def _fmt(v):
    if isinstance(v, dt.datetime):
        return v.strftime("%d.%m.%Y %H:%M:%S")
    if isinstance(v, dt.date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, dt.time):
        return v.strftime("%H:%M:%S")
    if hasattr(v, "is_finite") or isinstance(v, float):  # float or Decimal
        v = float(v)
        if v != v:  # NaN
            return None
        v = round(v, 2)
        return int(v) if v.is_integer() and abs(v) < 1e15 else v
    return v


def run_sql(con: duckdb.DuckDBPyConnection, sql: str) -> dict:
    sql = sql.strip().rstrip(";").strip()
    if not _SQL_START.match(sql):
        raise ValueError("only a single SELECT or WITH statement is allowed")
    if ";" in sql:
        raise ValueError("only one statement is allowed (remove ';')")
    timer = threading.Timer(SQL_TIMEOUT, con.interrupt)
    timer.start()
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description]
        raw = cur.fetchmany(MAX_ROWS + 1)
    except duckdb.InterruptException:
        raise ValueError(f"query took longer than {SQL_TIMEOUT:.0f} s; simplify it") from None
    finally:
        timer.cancel()
    rows = [[_fmt(v) for v in r] for r in raw[:MAX_ROWS]]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": len(raw) > MAX_ROWS}


# ============================================================ D. SYSTEM PROMPT

PROMPT_TEMPLATE = """You are an analyst of the UZEX commodity exchange deals registry (fictional data). You answer questions in Russian or Uzbek using ONLY rows returned by your SQL queries. No outside knowledge. Never guess, estimate or invent numbers, companies or products.

## Protocol
Reply with exactly ONE JSON object and nothing else:
{"lang":"uz|ru","action":"sql","sql":"SELECT ...","basis":"...","labels":{"alias":"..."},"answer_template":"..."}  -> I run it on DuckDB and reply "RESULT: {json}" or "SQL_ERROR: msg"
{"lang":"uz|ru","action":"answer","answer":"..."}                       -> final answer, only from RESULT rows
{"lang":"uz|ru","action":"not_found","reason":"..."}                    -> data has no answer; reason in the user's language
At most 4 queries. One statement, SELECT/WITH only, max 50 rows returned. DuckDB SQL dialect.
labels: a human-readable name for EVERY result column, in the user's language and script, e.g. {"cnt":"Bitimlar soni","total_uzs":"Jami summa, so‘m"} / {"cnt":"Количество сделок"}.
basis: ONE short line ("Key: value · Key: value") in the user's language and script that tells a non-technical user what data and filters the query uses, e.g. "Manba: bitimlar reyestri · tovar: Хлопок-сырец · davr: 2025-yil · holat: barchasi" / "Источник: реестр сделок · товар: Цемент · период: 2024 · статус: Исполнен, Исполнен(Частично)".
answer_template (saves a round trip — use it whenever possible): if your query returns exactly ONE row that fully answers the question, write the final answer with {column_alias} placeholders, e.g. "2024 yilda {cnt} ta bitim tuzilgan." I fill the values in exactly (numbers formatted). Put no numbers in the template except ones copied from the question. Give the result columns clear aliases. Omit answer_template when you must see the rows first (several rows, entity checks that may find nothing).

## Tables (DuckDB)
deals — one row per exchange deal (500 000, 03.01.2022–30.10.2026; states as of end of 30.10.2026)
  deal_no BIGINT; deal_date DATE; deal_time VARCHAR 'HH:MM:SS' (Tashkent); trade_type; qty DOUBLE = lots bought;
  amount DOUBLE = deal sum in contract currency; start_price DOUBLE = starting price per unit; contract_no VARCHAR;
  product_name = registry name with attributes ("… Марка … Сорт … Класс …"); product_name_norm = norm(product_name);
  lot DOUBLE = units per lot; unit; region = seller warehouse region (Uzbek Latin); contract_type; contract_form; status;
  seller_inn, buyer_inn VARCHAR(9) -> participants.inn; seller_broker, buyer_broker -> brokers.broker_code;
  pay_date DATE (NULL = not paid); delivery_date DATE (NULL = not delivered); delivered DOUBLE = lots delivered;
  from contract: currency, product (seller-typed base name), product_norm, product_family (canonical product, see list below), product_group, product_category, industry, hs_code;
  computed: volume = qty*lot (units), unit_price = amount/(qty*lot), usd_uzs = USD rate on deal_date, amount_uzs = amount in so‘m.
contracts — contract cards: contract_no VARCHAR, product, product_norm, product_family, brand, size, grade, ptype, pclass, hs_code, product_group,
  product_category, industry, unit, lot, currency, base_price (per unit at registration), deposit (% of deal sum), delivery_basis,
  region_ru (Russian), warehouse_address, trade_type, contract_type, contract_form, payment_days (days from deal to pay),
  delivery_days (days from payment to deliver), packaging, seller_inn, registered DATE, valid_until DATE.
participants — companies: inn VARCHAR(9), name, name_norm, region (Uzbek Latin), activity, role, vat_payer ('Да'/'Нет').
brokers — broker_code ('BK001'…'BK120'), name, name_norm, inn, region.
usd_rates — rate_date DATE, usd_uzs (so‘m per 1 USD), every day 01.01.2022–31.10.2026.
Joins: participants.inn = deals.seller_inn / buyer_inn; brokers.broker_code = deals.seller_broker / buyer_broker; contracts.contract_no = deals.contract_no.
norm(text) is a SQL function: lowercase, Cyrillic->Uzbek Latin, apostrophes removed, punctuation->space.

## Status meanings
Новый = concluded on 30.10.2026 (last day); Не подписан = not signed yet; Заключен = forward/futures signed, payment not yet due;
Ждёт оплату = spot signed, payment not yet due; Оплачен = paid, not delivered; Исполнен = paid and fully delivered;
Исполнен(Частично) = paid, partly delivered (see delivered); Аннулирован(Не оплачен) = not paid in time, cancelled = deal did NOT happen.

## Values
trade_type: {trade_type}
status: {status}
contract_type: {contract_type} | contract_form: {contract_form} | currency: {currency}
unit: {unit}
product_group: {product_group}
product_category: {product_category}
industry: {industry}
deals.region / participants.region: {region}
contracts.region_ru: {region_ru}
participants.activity: {activity} | role: {role}

## product_family — ALL products traded (family | Uzbek words | deal count). Every spelling variant is already merged.
{products}

## Rules
1. Products: map the user's word (Russian or Uzbek) to product_family from the list above and filter WHERE product_family = '…' — this already includes all spelling variants. "paxta" alone = 'Хлопок-сырец' (raw cotton); cotton oil/meal/yarn are separate families, include them only if asked. If the product is not in the list -> not_found (it was never traded). For a specific grade/brand/size (АИ-92, ЦЕМ I 42,5, 1 сорт, 4-класс …) add product_name_norm LIKE '%' || norm('…') || '%'. Spot/forward/futures = contract_form.
2. Companies, brokers, free text: compare with norm() on both sides: name_norm LIKE '%' || norm('Go‘qe Prokat') || '%'. INN = exact 9-digit string. Broker codes look like 'BK001'. Always show company name and INN.
3. Entity check first: if the question names a company, broker, deal number, contract number or product, your first query must confirm it exists (it may also compute the answer, e.g. SELECT p.name, p.inn, count(d.deal_no) … FROM participants p LEFT JOIN deals d … GROUP BY …). No matching row -> action MUST be not_found (not an answer saying "no deals").
4. Data covers deal dates 03.01.2022–30.10.2026. A period outside that -> not_found. "Now/today/this year" = 30.10.2026 / 2026.
5. Money is in the contract currency. Totals that mix currencies use amount_uzs and are in so‘m/сум. If all matched deals are USD, answer in USD (amount). Prices: compute per unit and currency separately; average price = sum(amount)/sum(volume) (weighted) unless the user asks otherwise; say which.
6. By default count all registry deals and state the filters used. "заключено/заключенных сделок/совершено/tuzilgan/bitim tuzilgan/bo‘lgan" = ALL deals (every row is a concluded exchange deal) — do NOT filter status='Заключен'; filter by a status value only when the user names that status explicitly. Words about outcome filter by status and you must say so: исполнено/исполненные/bajarilgan/ijro etilgan -> status IN ('Исполнен','Исполнен(Частично)') (ALWAYS both values: a partly executed deal is executed); поставлено/yetkazilgan -> delivered>0, delivered volume = delivered*lot; оплачено/to‘langan -> pay_date IS NOT NULL; продано/sotilgan/состоялось -> status <> 'Аннулирован(Не оплачен)'; аннулировано/bekor qilingan -> status = 'Аннулирован(Не оплачен)'.
7. Prefer ONE query that returns exactly the numbers you need (aggregates, GROUP BY, ORDER BY … LIMIT). Use year(deal_date), deal_date BETWEEN DATE '2024-01-01' AND DATE '2024-12-31'.
8. The answer: SAME language and script as the question (Uzbek Latin, Uzbek Cyrillic or Russian), 1–3 sentences. Copy numbers exactly from RESULT; write them with spaces as thousands separators and at most 2 decimals; always include units and currency. If RESULT has no rows or the question is not about this data -> not_found. Never answer with a number that is not in a RESULT.
9. Output JSON only."""


_PROMPT: str | None = None
_PROMPT_LOCK = threading.Lock()


def system_prompt() -> str:
    global _PROMPT
    with _PROMPT_LOCK:
        if _PROMPT is None:
            con = connect()
            try:
                def distinct(col: str, table: str = "deals") -> str:
                    rows = con.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL ORDER BY 1").fetchall()
                    return ", ".join(r[0] for r in rows)
                vals = {c: distinct(c) for c in ("trade_type", "status", "contract_type", "contract_form", "currency",
                                                 "unit", "product_group", "product_category", "industry", "region")}
                vals["region_ru"] = distinct("region_ru", "contracts")
                vals["activity"] = distinct("activity", "participants")
                vals["role"] = distinct("role", "participants")
                prods = con.execute("SELECT product_family, count(*) FROM deals GROUP BY 1 ORDER BY 2 DESC").fetchall()
                vals["products"] = "\n".join(f"{p} | {ALIASES.get(p, '')} | {n}" for p, n in prods)
            finally:
                con.close()
            prompt = PROMPT_TEMPLATE
            for k, v in vals.items():
                prompt = prompt.replace("{" + k + "}", v)
            _PROMPT = prompt
    return _PROMPT


_VARIANTS: dict[str, int] | None = None


def family_variants() -> dict[str, int]:
    """product_family -> number of distinct seller spellings merged into it."""
    global _VARIANTS
    if _VARIANTS is None:
        con = connect()
        try:
            _VARIANTS = dict(con.execute("SELECT product_family, count(DISTINCT product) FROM deals GROUP BY 1").fetchall())
        finally:
            con.close()
    return _VARIANTS


# ============================================================ C. AGENT LOOP

_UZ_CYR = re.compile(r"[ўқғҳЎҚҒҲ]")
_UZ_LAT = re.compile(r"[oOgG][‘’ʻʼ'`]")
_CYR = re.compile(r"[а-яёА-ЯЁ]")

MESSAGES = {
    "busy": {"ru": "Сервис временно занят, повторите через 10 секунд.",
             "uz": "Xizmat vaqtincha band, 10 soniyadan keyin qayta yuboring.",
             "uz_cyr": "Хизмат вақтинча банд, 10 сониядан кейин қайта юборинг."},
    "timeout": {"ru": "Не удалось завершить анализ за отведённое время. Переформулируйте вопрос короче.",
                "uz": "Tahlilni belgilangan vaqtda yakunlab bo‘lmadi. Savolni qisqaroq qilib qayta yuboring.",
                "uz_cyr": "Таҳлилни белгиланган вақтда якунлаб бўлмади. Саволни қисқароқ қилиб қайта юборинг."},
    "bad_reply": {"ru": "Не удалось получить корректный ответ. Повторите вопрос.",
                  "uz": "To‘g‘ri javob olinmadi. Savolni qayta yuboring.",
                  "uz_cyr": "Тўғри жавоб олинмади. Саволни қайта юборинг."},
}


def detect_lang(text: str) -> str:
    """'ru' | 'uz' | 'uz_cyr' (Uzbek in Cyrillic). Latin text without markers is treated as Uzbek."""
    if _UZ_CYR.search(text):
        return "uz_cyr"
    if _CYR.search(text):
        return "ru"
    return "uz"


_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_DATE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b")
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _squash(text: str) -> str:
    """'1 091 975,50' -> '1091975.50' (digit-group spaces removed, decimal comma -> dot)."""
    text = re.sub(r"(?<=\d)[\s  ](?=\d{3})", "", text)
    return re.sub(r"(?<=\d),(?=\d)", ".", text)


def fmt_value(v) -> str:
    if isinstance(v, bool) or v is None:
        return str(v)
    if isinstance(v, int):
        return f"{v:,}".replace(",", " ")
    if isinstance(v, float):
        return f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return str(v)


def fill_template(template: str, res: dict, question: str) -> str | None:
    """Fill {col} placeholders from a single-row RESULT. None = cannot be used safely -> ask the model."""
    if res.get("row_count") != 1 or not template:
        return None
    row = dict(zip(res["columns"], res["rows"][0]))
    names = _PLACEHOLDER.findall(template)
    if not names or any(n not in row for n in names):
        return None
    if any(row[n] is None or row[n] == 0 for n in names):  # empty/zero: let the model decide (maybe not_found)
        return None
    # the template itself may only contain numbers that were in the question
    q_nums = set(_NUM.findall(_squash(question)))
    if any(n not in q_nums for n in _NUM.findall(_squash(_PLACEHOLDER.sub("", template)))):
        return None
    return _PLACEHOLDER.sub(lambda m: fmt_value(row[m.group(1)]), template)


def unknown_numbers(answer_text: str, evidence: list[dict], question: str) -> list[str]:
    """Numbers in the answer that are not in any RESULT (or the question). Small integers are allowed."""
    known: list[float] = []
    for ev in evidence:
        for row in ev.get("rows", []):
            for v in row:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    known.append(float(v))
                elif isinstance(v, str):
                    known += [float(x.replace(",", ".")) for x in _NUM.findall(_squash(_DATE.sub("", v)))]
    known += [float(x) for x in _NUM.findall(_squash(_DATE.sub("", question)))]
    bad = []
    for tok in _NUM.findall(_squash(_DATE.sub("", answer_text))):
        x = float(tok.replace(",", "."))
        if x < 100 or (2022 <= x <= 2026 and x.is_integer()):
            continue
        if not any(abs(x - k) < 0.0051 or (x.is_integer() and x == int(k)) or x == round(k) for k in known):
            bad.append(tok)
    return bad


def parse_json(text: str) -> dict | None:
    """Strip ``` fences and parse the first balanced {...} (string-aware)."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1], strict=False)
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def answer(question: str) -> dict:
    t0 = time.monotonic()
    deadline = t0 + TOTAL_BUDGET
    detected = detect_lang(question)
    base_lang = "ru" if detected == "ru" else "uz"
    evidence: list[dict] = []
    provider = ""

    def result(status: str, text: str, lang: str = base_lang) -> dict:
        return {"lang": lang, "text": text, "evidence": evidence, "status": status,
                "provider": provider, "seconds": round(time.monotonic() - t0, 2)}

    msgs = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": question}]
    con = connect()
    sql_steps, json_retry, number_guard, unknown_guard = 0, False, False, False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining < 3:
                return result("error", MESSAGES["timeout"][detected])
            try:
                text, provider = call_llm(msgs, timeout=min(LLM_TIMEOUT, remaining))
            except LLMUnavailable:
                # every provider is rate-limited / overloaded: wait a little and retry while the budget allows
                if remaining > RETRY_MIN_REMAINING:
                    time.sleep(RETRY_WAIT)
                    continue
                return result("error", MESSAGES["busy"][detected])

            obj = parse_json(text)
            action = obj.get("action") if obj else None
            if action not in ("sql", "answer", "not_found"):
                if json_retry:
                    return result("error", MESSAGES["bad_reply"][detected])
                json_retry = True
                msgs += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Reply with valid JSON only."}]
                continue

            lang = obj.get("lang") if obj.get("lang") in ("uz", "ru") else base_lang
            msgs.append({"role": "assistant", "content": json.dumps(obj, ensure_ascii=False)})

            if action == "answer":
                ans = str(obj.get("answer", "")).strip()
                # guard: a number in the answer without any query behind it is a hallucination risk
                if not evidence and re.search(r"\d", ans) and not number_guard:
                    number_guard = True
                    msgs.append({"role": "user", "content": "You have not run any SQL. Numbers must come from a "
                                 "RESULT. Run a query, or reply not_found."})
                    continue
                bad = unknown_numbers(ans, evidence, question) if evidence else []
                if bad and not unknown_guard:
                    unknown_guard = True
                    log.warning("answer has numbers not in RESULT: %s", bad)
                    msgs.append({"role": "user", "content": f"These numbers are not in any RESULT: {', '.join(bad)}. "
                                 "Copy numbers exactly from RESULT (no rounding, no arithmetic) and reply again, "
                                 "or run a query that returns the needed number."})
                    continue
                return result("ok", ans, lang)

            if action == "not_found":
                return result("not_found", str(obj.get("reason", "")).strip(), lang)

            # action == "sql"
            if sql_steps >= MAX_SQL_STEPS:
                msgs.append({"role": "user", "content": "Query limit reached. Reply now with action answer "
                             "(only from RESULTs above) or not_found."})
                if sql_steps > MAX_SQL_STEPS:  # it ignored that once already
                    return result("error", MESSAGES["timeout"][detected])
                sql_steps += 1
                continue
            sql_steps += 1
            sql = str(obj.get("sql", ""))
            labels = obj.get("labels") if isinstance(obj.get("labels"), dict) else {}
            step = {"sql": sql, "basis": str(obj.get("basis") or obj.get("purpose") or ""),
                    "labels": {str(k): str(v) for k, v in labels.items()}}
            try:
                res = run_sql(con, sql)
                step.update(res)
                payload = json.dumps(res, ensure_ascii=False, default=str)
                if len(payload) > 8000:
                    payload = payload[:8000] + "…(cut)"
                reply = "RESULT: " + payload
            except Exception as e:  # noqa: BLE001 - give the error back so the model can fix the query
                step.update({"columns": [], "rows": [], "row_count": 0, "error": str(e)[:500]})
                reply = "SQL_ERROR: " + str(e)[:500]
            evidence.append(step)
            log.info("sql step=%d rows=%s err=%s", sql_steps, step.get("row_count"), step.get("error"))
            if not step.get("error"):
                filled = fill_template(str(obj.get("answer_template") or ""), step, question)
                if filled:  # one-round-trip answer: values copied by code straight from the RESULT row
                    return result("ok", filled, lang)
            if sql_steps == MAX_SQL_STEPS:
                reply += "\nThat was the last allowed query. Now reply with answer or not_found."
            msgs.append({"role": "user", "content": reply})
    finally:
        con.close()


# ============================================================ E. CLI

def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if argv[1:2] == ["--check-llm"]:
        check_llm()
        return
    if argv[1:2] == ["--prompt"]:
        print(system_prompt())
        return
    if len(argv) < 2:
        sys.exit(__doc__)
    r = answer(" ".join(argv[1:]))
    print("\n" + r["text"])
    print(f"\nstatus={r['status']} lang={r['lang']} provider={r['provider']} seconds={r['seconds']}")
    for i, ev in enumerate(r["evidence"], 1):
        print(f"\n--- SQL {i}: {ev.get('basis', '')}\n{ev['sql']}")
        if ev.get("error"):
            print(f"ERROR: {ev['error']}")
        else:
            print(f"{ev['columns']}  rows={ev['row_count']}")
            for row in ev["rows"][:5]:
                print(f"  {row}")


if __name__ == "__main__":
    main(sys.argv)
