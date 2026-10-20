"""Собрать migrations/002_seed.sql из seed/*.csv — перенос вводных финмодели v1.5.

    python analytics/fin/seed_sql.py

Запускается один раз, результат коммитится: миграции не переписываются. Если
seed поменяется до применения на бою — пересобрать и закоммитить заново.

Вставки идемпотентны: справочники — on conflict do nothing, вводные и
события — только если таких строк из импорта ещё нет.
"""

from __future__ import annotations

import csv
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = HERE / "seed"
OUT = HERE / "migrations" / "002_seed.sql"
IMPORT = "импорт v1.5"

# Как товар называется в каталоге бота (позиция заказа — «<имя> 100 г»).
# Источник — ref_products.bot_name на 20.10.2026: бот продаёт три сорта.
BOT_NAMES = {
    "Те Гуань Инь ЭКО (XFI0044)": "Те Гуань Инь",
    "Да Хун Пао, сильная прожарка (XFI0005)": "Да Хун Пао",
    "Дянь Хунь фермерский (XFI0095)": "Дянь Хун",
}


def q(value: str | None) -> str:
    if value is None or value == "":
        return "null"
    return "'" + value.replace("'", "''") + "'"


def num(value: str) -> str:
    value = (value or "").strip().replace(",", ".")
    if value == "":
        return "null"
    float(value)  # не число — пусть упадёт здесь, а не на базе
    return value


def rows(name: str) -> list[dict]:
    with open(SEED / name, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def is_number(value: str) -> bool:
    try:
        float(value.replace(",", "."))
    except ValueError:
        return False
    return True


def main() -> None:
    out = [
        "-- 002: данные из финмодели v1.5 (seed/*.csv). Собрано analytics/fin/seed_sql.py — не править руками.",
        "",
    ]

    out.append("insert into fin.sku (sku_name, category, supplier_unit, unit_weight_kg, price_per_unit_base,")
    out.append("                     qty_units_first_purchase, sale_pack_g, retail_price, role, bot_name, entered_by) values")
    values = []
    for r in rows("fin_sku.csv"):
        values.append(
            f"  ({q(r['sku_name'])}, {q(r['category'])}, {q(r['supplier_unit'])}, {num(r['unit_weight_kg'])}, "
            f"{num(r['price_per_unit_base'])}, {num(r['qty_units_first_purchase'])}, {num(r['sale_pack_g'])}, "
            f"{num(r['retail_price'])}, {q(r['role'])}, {q(BOT_NAMES.get(r['sku_name']))}, {q(IMPORT)})"
        )
    out.append(",\n".join(values) + "\non conflict (sku_name) do nothing;\n")

    out.append("insert into fin.bundles (bundle, role, retail_price, pouches, pouch_type, codes_total, labels_total, entered_by) values")
    values = [
        f"  ({q(r['bundle'])}, {q(r['role'])}, {num(r['retail_price'])}, {int(r['pouches'])}, {q(r['pouch_type'])}, "
        f"{int(r['codes_total'])}, {int(r['labels_total'])}, {q(IMPORT)})"
        for r in rows("fin_bundles.csv")
    ]
    out.append(",\n".join(values) + "\non conflict (bundle) do nothing;\n")

    out.append("insert into fin.bundle_items (bundle, sku_name, grams) values")
    values = [f"  ({q(r['bundle'])}, {q(r['sku_name'])}, {num(r['grams'])})" for r in rows("fin_bundle_items.csv")]
    out.append(",\n".join(values) + "\non conflict do nothing;\n")

    out.append("insert into fin.inputs (key, label, value, value_text, unit, status, note, valid_from, entered_by)")
    out.append("select * from (values")
    values = []
    for r in rows("fin_inputs.csv"):
        raw = (r["value"] or "").strip()
        # Даты и прочий текст — в value_text; пусто — «нет данных» (NULL, не 0).
        value, text = (num(raw), "null") if raw and is_number(raw) else ("null", q(raw))
        values.append(
            f"  ({q(r['key'])}, {q(r['label'])}, {value}::numeric, {text}, {q(r['unit'])}, {q(r['status'])}, "
            f"{q(r['note'])}, {q(r['valid_from'])}::date, {q(IMPORT)})"
        )
    out.append(",\n".join(values))
    out.append(") as v (key, label, value, value_text, unit, status, note, valid_from, entered_by)")
    out.append(f"where not exists (select 1 from fin.inputs where entered_by = {q(IMPORT)});\n")

    out.append("insert into fin.expenses (date, category, amount, description, is_plan, entered_by)")
    out.append("select * from (values")
    values = [
        f"  ({q(r['date'])}::date, {q(r['category'])}, {num(r['amount'])}::numeric, {q(r['description'])}, "
        f"{r['is_plan'].strip().lower() == 'true'}, {q(r['entered_by'])})"
        for r in rows("fin_expenses.csv")
    ]
    out.append(",\n".join(values))
    out.append(") as v (date, category, amount, description, is_plan, entered_by)")
    out.append("where not exists (select 1 from fin.expenses);\n")

    out.append("insert into fin.cash_snapshots (date, balance, source, note, entered_by)")
    out.append("select * from (values")
    values = [
        f"  ({q(r['date'])}::date, {num(r['balance'])}::numeric, {q(r['source'])}, {q(r['note'])}, {q(IMPORT)})"
        for r in rows("fin_cash_snapshots.csv")
    ]
    out.append(",\n".join(values))
    out.append(") as v (date, balance, source, note, entered_by)")
    out.append("where not exists (select 1 from fin.cash_snapshots);\n")

    out.append("insert into fin.schema_migrations (version) values ('002_seed') on conflict do nothing;")
    OUT.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"{OUT.relative_to(HERE.parent.parent)}: {OUT.stat().st_size} байт")


if __name__ == "__main__":
    main()
