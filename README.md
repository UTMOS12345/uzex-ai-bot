# UZEX deals agent (Telegram bot)

Answers questions about UZEX exchange deals in Russian and Uzbek **only from the registry data**:
question → LLM writes one DuckDB SQL query → the answer is filled in from the query result → the bot shows the answer,
what it is based on (filters, result table, SQL) and the time taken.

| File | Role |
|---|---|
| `build_db.py` | CSV → `uzex.duckdb` (typed columns, USD→UZS, normalized names, product families) |
| `textnorm.py` | `norm()`: Cyrillic/Latin Uzbek + Russian → one Latin form, for company/product matching |
| `products.py` | 229 seller-typed product names → 42 product families with Uzbek names |
| `agent.py` | LLM failover chain, safe read-only SQL runner, agent loop, answer templates, number check |
| `bot.py` | aiogram 3 bot (long polling) |
| `eval.py` | 18 judge-style questions with SQL-computed reference answers |

## Run locally
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
unzip uzex_spot_data.zip && .venv/bin/python build_db.py uzex_spot_data uzex.duckdb
cp .env.example .env   # fill BOT_TOKEN and LLMn_API_KEY
.venv/bin/python agent.py --check-llm
.venv/bin/python agent.py "2025-yilda paxta bo‘yicha nechta bitim tuzilgan?"
.venv/bin/python eval.py
.venv/bin/python bot.py
```
`.env` values that contain JSON must be in single quotes (systemd strips double quotes).

## Deploy (Ubuntu, systemd)
See `deploy/uzex-bot.service`; the app lives in `/opt/uzex-bot` and runs as user `uzex`.
