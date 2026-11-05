"""Build uzex.duckdb from the CSV folder.  Usage: python build_db.py uzex_spot_data uzex.duckdb"""
import os
import sys

import duckdb

from products import family
from textnorm import norm


def num(col: str) -> str:
    """'78 356 027.52' -> 78356027.52 (spaces, no-break spaces and '%' removed; empty -> NULL)."""
    return (f"TRY_CAST(NULLIF(regexp_replace(\"{col}\", '[\\s\u00a0\u202f%]', '', 'g'), '') AS DOUBLE)")


def dt(col: str) -> str:
    return f"CAST(try_strptime(NULLIF(trim(\"{col}\"), ''), '%d.%m.%Y') AS DATE)"


def main(src: str, out: str) -> None:
    if os.path.exists(out):
        os.remove(out)
    con = duckdb.connect(out)
    con.create_function("norm", norm, ["VARCHAR"], "VARCHAR", null_handling="special")
    con.create_function("family", family, ["VARCHAR"], "VARCHAR", null_handling="special")

    def csv(name: str) -> str:
        path = os.path.join(src, name).replace("'", "''")
        return f"read_csv('{path}', all_varchar=true, header=true, delim=',', quote='\"')"

    con.execute(f"""
        CREATE TABLE usd_rates AS
        SELECT {dt('Дата')} AS rate_date, {num('Курс')} AS usd_uzs
        FROM {csv('usd_rates.csv')}
    """)

    con.execute(f"""
        CREATE TABLE participants AS
        SELECT "ИНН" AS inn, "Наименование" AS name, norm("Наименование") AS name_norm,
               "Регион" AS region, "Вид деятельности" AS activity, "Роль" AS role,
               "Плательщик НДС" AS vat_payer
        FROM {csv('participants.csv')}
    """)

    con.execute(f"""
        CREATE TABLE brokers AS
        SELECT "Код брокера" AS broker_code, "Наименование" AS name, norm("Наименование") AS name_norm,
               "ИНН" AS inn, "Регион" AS region
        FROM {csv('brokers.csv')}
    """)

    con.execute(f"""
        CREATE TABLE contracts AS
        SELECT "Номер контракта" AS contract_no,
               "Наименование продукта" AS product, norm("Наименование продукта") AS product_norm,
               family("Наименование продукта") AS product_family,
               "Марка товара" AS brand, "Размер товара" AS size, "Сорт товара" AS grade,
               "Тип товара" AS ptype, "Класс товара" AS pclass, "Код ТН ВЭД" AS hs_code,
               "Группа товара" AS product_group, "Категория товара" AS product_category,
               "Отрасль промышленности" AS industry, "Единица измерения" AS unit,
               {num('Лот')} AS lot, "Валюта" AS currency, {num('Базисная цена')} AS base_price,
               {num('Сумма залога')} AS deposit, "Базис поставки" AS delivery_basis,
               "Регион" AS region_ru, "Адрес склада" AS warehouse_address,
               "Тип торгов" AS trade_type, "Тип контракта" AS contract_type,
               "Форма контракта" AS contract_form,
               CAST({num('Срок оплаты')} AS INTEGER) AS payment_days,
               CAST({num('Срок поставки')} AS INTEGER) AS delivery_days,
               "Упаковка" AS packaging, "ИНН продавца" AS seller_inn,
               {dt('Дата регистрации')} AS registered, {dt('Действует до')} AS valid_until
        FROM {csv('contracts.csv')}
    """)

    deals_glob = os.path.join(src, "deals_*.csv").replace("'", "''")
    con.execute(f"""
        CREATE TABLE deals AS
        WITH d AS (
            SELECT CAST("Номер сделки" AS BIGINT) AS deal_no, {dt('Дата сделки')} AS deal_date,
                   "Время сделки" AS deal_time, "Тип торгов" AS trade_type,
                   {num('Количество')} AS qty, {num('Сумма сделки')} AS amount,
                   {num('Стартовая цена')} AS start_price, "Номер контракта" AS contract_no,
                   "Наименование товара" AS product_name, {num('Лот')} AS lot,
                   "Единица измерения" AS unit, "Регион" AS region,
                   "Тип контракта" AS contract_type, "Форма контракта" AS contract_form,
                   "Состояние сделки" AS status, "ИНН продавца" AS seller_inn,
                   "ИНН покупателя" AS buyer_inn, "Брокер продавца" AS seller_broker,
                   "Брокер покупателя" AS buyer_broker, {dt('Дата оплаты')} AS pay_date,
                   {dt('Дата поставки')} AS delivery_date, {num('Поставлено')} AS delivered
            FROM read_csv('{deals_glob}', all_varchar=true, header=true, delim=',', quote='"')
        ),
        names AS (SELECT DISTINCT product_name FROM d)
        SELECT d.*,
               n.product_name_norm,
               c.currency, c.product, c.product_norm, c.product_family, c.product_group, c.product_category,
               c.industry, c.hs_code,
               d.qty * d.lot AS volume,
               d.amount / NULLIF(d.qty * d.lot, 0) AS unit_price,
               r.usd_uzs,
               CASE WHEN c.currency = 'Доллар США' THEN d.amount * r.usd_uzs ELSE d.amount END AS amount_uzs
        FROM d
        LEFT JOIN (SELECT product_name, norm(product_name) AS product_name_norm FROM names) n USING (product_name)
        LEFT JOIN contracts c ON c.contract_no = d.contract_no
        LEFT JOIN usd_rates r ON r.rate_date = d.deal_date
        ORDER BY d.deal_no
    """)

    for t in ["deals", "contracts", "participants", "brokers", "usd_rates"]:
        print(f"{t:13s} {con.execute(f'SELECT count(*) FROM {t}').fetchone()[0]}")

    # sanity checks: nothing should silently fail to parse
    checks = {
        "deals without contract": "SELECT count(*) FROM deals WHERE currency IS NULL",
        "deals without usd rate": "SELECT count(*) FROM deals WHERE usd_uzs IS NULL",
        "deals with NULL amount/qty/date": "SELECT count(*) FROM deals WHERE amount IS NULL OR qty IS NULL OR deal_date IS NULL",
        "USD deals": "SELECT count(*) FROM deals WHERE currency = 'Доллар США'",
    }
    for label, q in checks.items():
        print(f"{label}: {con.execute(q).fetchone()[0]}")
    con.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
