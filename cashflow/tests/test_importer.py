from pathlib import Path

from cashflow.importer import (
    read_csv,
    validate_articles,
    validate_loan_schedule,
    validate_rules,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def test_shipped_csv_files_are_valid():
    """Справочники в репозитории должны проходить валидацию как есть."""
    articles = read_csv(DATA_DIR / "articles.csv")
    rules = read_csv(DATA_DIR / "rules.csv")
    loan = read_csv(DATA_DIR / "loan_schedule.csv")

    assert validate_articles(articles) == []
    assert validate_rules(rules, {a["article_id"] for a in articles}) == []
    assert validate_loan_schedule(loan) == []


def test_comments_are_skipped(tmp_path: Path):
    path = tmp_path / "rules.csv"
    path.write_text(
        "priority,field,match_type,value,article_id\n"
        "# это комментарий\n"
        "10,purpose,contains,чай,cfo_out_goods\n",
        encoding="utf-8",
    )
    rows = read_csv(path)
    assert len(rows) == 1
    assert rows[0]["value"] == "чай"


def test_unknown_article_is_rejected():
    rows = [{"priority": "10", "field": "purpose", "match_type": "contains",
             "value": "чай", "article_id": "нет_такой_статьи"}]
    errors = validate_rules(rows, {"cfo_out_goods"})
    assert any("неизвестная статья" in e for e in errors)


def test_broken_regex_is_rejected():
    rows = [{"priority": "10", "field": "purpose", "match_type": "regex",
             "value": "заказ [", "article_id": "cfo_out_goods"}]
    errors = validate_rules(rows, {"cfo_out_goods"})
    assert any("битый regex" in e for e in errors)


def test_bad_priority_is_rejected():
    rows = [{"priority": "первый", "field": "purpose", "match_type": "contains",
             "value": "чай", "article_id": "cfo_out_goods"}]
    errors = validate_rules(rows, {"cfo_out_goods"})
    assert any("priority" in e for e in errors)


def test_article_with_unknown_section_is_rejected():
    rows = [{"article_id": "x", "article": "X", "section": "XXX", "direction": "in"}]
    assert any("section" in e for e in validate_articles(rows))


def test_required_articles_must_exist():
    rows = [{"article_id": "x", "article": "X", "section": "CFO", "direction": "in"}]
    errors = validate_articles(rows)
    assert any("tech_unclassified" in e for e in errors)


def test_loan_row_where_parts_do_not_sum_is_rejected():
    rows = [{"period_no": "1", "due_date": "2025-02-10", "payment_total": "9601.74",
             "interest": "2250.00", "principal": "7000.00"}]
    errors = validate_loan_schedule(rows)
    assert any("не равно платежу" in e for e in errors)
