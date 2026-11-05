"""Regression test: judge-style questions with reference answers computed by SQL.

Usage: python eval.py [provider numbers, e.g. 3 or 1,3]   (default: all providers in .env order)
A case passes if status matches and every expected value appears in the answer text.
"""
import logging
import re
import sys
import time

import agent
from textnorm import norm

con = agent.connect()


def one(sql):
    return con.execute(sql).fetchone()


def num(x, decimals=0):
    """Expected number as it should appear once spaces are removed: 1091975301500 / 11720.49"""
    return f"{x:.{decimals}f}"


CASES = [
    ("Сколько сделок было заключено в 2024 году?",
     [num(one("SELECT count(*) FROM deals WHERE year(deal_date)=2024")[0])]),
    ("2025-yilda paxta bo‘yicha nechta bitim tuzilgan?",
     [num(one("SELECT count(*) FROM deals WHERE year(deal_date)=2025 AND product_family='Хлопок-сырец'")[0])]),
    ("Какая общая сумма исполненных сделок по цементу в 2024 году?",
     [num(one("SELECT sum(amount_uzs) FROM deals WHERE year(deal_date)=2024 AND product_family='Цемент' "
              "AND status IN ('Исполнен','Исполнен(Частично)')")[0])]),
    ("2023-yilda savdo summasi bo‘yicha eng yirik sotuvchi kim?",
     [{"name": one("SELECT p.name FROM deals d JOIN participants p ON p.inn=d.seller_inn WHERE year(deal_date)=2023 "
                   "GROUP BY 1 ORDER BY sum(amount_uzs) DESC LIMIT 1")[0]}]),
    ("Кто покупатель по сделке №250000 и какова её сумма?",
     [{"name": one("SELECT p.name FROM deals d JOIN participants p ON p.inn=d.buyer_inn WHERE deal_no=250000")[0]},
      num(one("SELECT amount FROM deals WHERE deal_no=250000")[0])]),
    ("BK015 broker nechta bitimda ishtirok etgan?",
     [num(one("SELECT count(*) FROM deals WHERE seller_broker='BK015' OR buyer_broker='BK015'")[0])]),
    ("Какая средняя цена карбамида за тонну в 2025 году?",
     [num(int(one("SELECT sum(amount)/sum(volume) FROM deals WHERE year(deal_date)=2025 AND product_family='Карбамид'")[0]))]),
    ("Сколько экспортных сделок было в 2026 году и на какую сумму в долларах?",
     [num(one("SELECT count(*) FROM deals WHERE year(deal_date)=2026 AND contract_type='Экспортный'")[0]),
      num(int(one("SELECT sum(amount) FROM deals WHERE year(deal_date)=2026 AND contract_type='Экспортный'")[0]))]),
    ("Qaysi hududda eng ko‘p bitim tuzilgan?",
     [{"any": ["toshkent shahri", "toshkent shahar", "tashkent", "g toshkent"]},
      num(one("SELECT count(*) FROM deals WHERE region='Toshkent shahri'")[0])]),
    ("Сколько сделок было аннулировано в 2023 году?",
     [num(one("SELECT count(*) FROM deals WHERE year(deal_date)=2023 AND status='Аннулирован(Не оплачен)'")[0])]),
    ("2024-yil 15-martdagi dollar kursi qancha edi?",
     [num(one("SELECT usd_uzs FROM usd_rates WHERE rate_date=DATE '2024-03-15'")[0], 2)]),
    ("2024 йилда бензин бўйича нечта битим тузилган?",
     [num(one("SELECT count(*) FROM deals WHERE year(deal_date)=2024 AND product_family='Бензин'")[0])]),
    ("\"Go‘qe Prokat\" MChJ jami qancha summaga mahsulot sotgan?",
     [num(int(one("SELECT sum(amount_uzs) FROM deals d JOIN participants p ON p.inn=d.seller_inn "
                  "WHERE p.name_norm LIKE '%goqe prokat%' AND d.status <> 'Аннулирован(Не оплачен)'")[0]))]),
    ("2025-yilda Qashqadaryo viloyatida bug‘doy bo‘yicha bitimlar hajmi necha tonna?",
     [num(one("SELECT sum(volume) FROM deals WHERE year(deal_date)=2025 AND product_family='Пшеница' "
              "AND region='Qashqadaryo viloyati'")[0])]),
    ("Сколько сделок по бензину АИ-92?",
     [num(one("SELECT count(*) FROM deals WHERE product_family='Бензин' AND product_name_norm LIKE '%ai 92%'")[0])]),
    ("Сколько сделок у компании ООО «Рога и копыта»?", "not_found"),
    ("Сколько сделок было в 2021 году?", "not_found"),
    ("Kofe bo‘yicha nechta bitim bor?", "not_found"),
]


def squash(text: str) -> str:
    """Remove digit-group spaces and turn decimal commas into dots: '1 091 975,50' -> '1091975.50'."""
    t = re.sub(r"(?<=\d)[\s  ](?=\d)", "", text)
    return re.sub(r"(?<=\d),(?=\d)", ".", t)


def check(r: dict, expected) -> tuple[bool, str]:
    if expected == "not_found":
        return r["status"] == "not_found", "expected not_found"
    if r["status"] != "ok":
        return False, f"status={r['status']}"
    text, ntext = squash(r["text"]), norm(r["text"])
    for e in expected:
        if isinstance(e, dict) and "name" in e:
            if norm(e["name"]).replace(" mchj", "").strip() not in ntext:
                return False, f"missing name {e['name']}"
        elif isinstance(e, dict):
            if not any(a in ntext for a in e["any"]):
                return False, f"missing one of {e['any']}"
        elif not re.search(rf"(?<![\d.]){re.escape(e)}(?!\d)", text):
            return False, f"missing {e}"
    return True, ""


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if len(sys.argv) > 1:
        keep = {f"LLM{n}:" for n in sys.argv[1].split(",")}
        agent.PROVIDERS[:] = [p for p in agent.PROVIDERS if any(p.name.startswith(k) for k in keep)]
    print("providers:", [p.name for p in agent.PROVIDERS])
    passed, times = 0, []
    t_all = time.monotonic()
    for i, (q, exp) in enumerate(CASES, 1):
        r = agent.answer(q)
        ok, why = check(r, exp)
        passed += ok
        times.append(r["seconds"])
        print(f"{'PASS' if ok else 'FAIL'} {i:2d} {r['seconds']:5.1f}s sql={len(r['evidence'])} "
              f"{r['provider']:28s} {q[:60]}")
        print(f"        -> {r['text'][:220]}")
        if not ok:
            print(f"        !! {why}; expected {exp}")
    print(f"\n{passed}/{len(CASES)} passed | max {max(times):.1f}s | avg {sum(times)/len(times):.1f}s "
          f"| total {time.monotonic() - t_all:.0f}s")


if __name__ == "__main__":
    main()
