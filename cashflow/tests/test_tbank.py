from datetime import date
from decimal import Decimal

import httpx
import pytest

from cashflow.tbank import TBankClient, TBankError, parse_operation

ACCOUNT = "40802810100000000001"


def op_payload(**kwargs):
    payload = {
        "operationId": "1",
        "operationStatus": "Transaction",
        "chargeDate": "2025-03-10T09:15:00+03:00",
        "typeOfOperation": "Debit",
        "accountAmount": "1234.56",
        "recipientName": "ООО Поставщик",
        "recipientInn": "7712345678",
        "paymentPurpose": "Оплата по счету 1",
    }
    payload.update(kwargs)
    return payload


def test_parse_outgoing_operation():
    op = parse_operation(op_payload(), ACCOUNT)
    assert op.direction == "out"
    assert op.amount == Decimal("1234.56")
    assert op.counterparty_inn == "7712345678"
    assert op.operation_date == date(2025, 3, 10)


def test_incoming_operation_takes_payer_as_counterparty():
    op = parse_operation(
        op_payload(typeOfOperation="Credit", payerName="ИП Петров", payerInn="771234567890"),
        ACCOUNT,
    )
    assert op.direction == "in"
    assert op.counterparty_name == "ИП Петров"


def test_late_night_utc_operation_keeps_moscow_date():
    # 23:30 UTC 9 марта — это уже 10 марта по Москве.
    op = parse_operation(op_payload(chargeDate="2025-03-09T23:30:00Z"), ACCOUNT)
    assert op.operation_date == date(2025, 3, 10)


def test_direction_is_derived_from_account_when_marker_missing():
    payload = op_payload(payerAccount=ACCOUNT)
    del payload["typeOfOperation"]
    assert parse_operation(payload, ACCOUNT).direction == "out"


def test_unknown_direction_raises_instead_of_guessing():
    payload = op_payload()
    del payload["typeOfOperation"]
    with pytest.raises(TBankError):
        parse_operation(payload, ACCOUNT)


def _client(handler) -> TBankClient:
    transport = httpx.MockTransport(handler)
    return TBankClient("token", "https://example.test/openapi",
                       client=httpx.Client(transport=transport))


def test_pagination_follows_cursor_and_reads_balances_once():
    seen_params = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={
                "openingBalance": "10000.00",
                "closingBalance": "12345.67",
                "operations": [op_payload(operationId="a")],
                "nextCursor": "page2",
            })
        return httpx.Response(200, json={"operations": [op_payload(operationId="b")]})

    with _client(handler) as client:
        pages = list(client.statement(ACCOUNT, date(2025, 3, 1), date(2025, 3, 31)))

    assert [op.operation_id for ops, _ in pages for op in ops] == ["a", "b"]
    assert len(pages[0][1]) == 2  # opening и closing
    assert pages[1][1] == []
    assert seen_params[0]["withBalances"] == "true"
    assert seen_params[1]["cursor"] == "page2"
    assert int(seen_params[0]["limit"]) == 5000


def test_non_transaction_statuses_are_skipped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"operations": [
            op_payload(operationId="hold", operationStatus="Authorization"),
            op_payload(operationId="real", operationStatus="Transaction"),
        ]})

    with _client(handler) as client:
        operations = [op for ops, _ in client.statement(ACCOUNT, date(2025, 3, 1), date(2025, 3, 2))
                      for op in ops]

    assert [op.operation_id for op in operations] == ["real"]


def test_server_error_is_retried(monkeypatch):
    monkeypatch.setattr("cashflow.tbank.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"operations": []})

    with _client(handler) as client:
        list(client.statement(ACCOUNT, date(2025, 3, 1), date(2025, 3, 2)))

    assert calls["n"] == 3


def test_forbidden_is_not_retried(monkeypatch):
    monkeypatch.setattr("cashflow.tbank.time.sleep", lambda _seconds: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")

    with _client(handler) as client:
        with pytest.raises(TBankError, match="отказал в доступе"):
            list(client.statement(ACCOUNT, date(2025, 3, 1), date(2025, 3, 2)))

    assert calls["n"] == 1


def test_token_never_appears_in_error_text(monkeypatch):
    monkeypatch.setattr("cashflow.tbank.time.sleep", lambda _seconds: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="denied")

    client = TBankClient("SUPER-SECRET-TOKEN", "https://example.test/openapi",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    with client:
        with pytest.raises(TBankError) as exc:
            list(client.statement(ACCOUNT, date(2025, 3, 1), date(2025, 3, 2)))
    assert "SUPER-SECRET-TOKEN" not in str(exc.value)
