"""Живое сравнение моделей и уровней effort на типичных ходах бота.

Те же системный промпт и инструменты, что у бота; данные клиентов — только
метками. На каждый случай — один запрос без продолжения хода: меряем время
ответа, токены, stop_reason (в том числе отказы) и что модель вызвала.
Время — до полного ответа, а не до первого токена: у вебхука ВК около
8 секунд на весь ход, и важно именно оно.

    python scripts/model_compare.py claude-sonnet-5:medium claude-sonnet-5-5:medium claude-sonnet-5-5:low
    python scripts/model_compare.py --runs 2 claude-sonnet-5-5:low

Нужен ANTHROPIC_API_KEY (и ANTHROPIC_BASE_URL, если ходим через прокси).
База не нужна. Каждый запрос платный — около цента.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.pii_model_check import CASES as RECIPIENT_CASES, CATALOG, LABELS, build  # noqa: E402

# (название, реплика клиента, что ждём: имя инструмента или None — ответ словами)
CONSULT_CASES = [
    ("совет на вечер", "Привет! Посоветуйте чай на вечер, чтобы не бодрил", None),
    ("сравнение сортов", "Чем Да Хун Пао отличается от Те Гуань Инь? Что выбрать новичку?", None),
    ("заказ сразу", "Хочу Те Гуань Инь 100 г, доставка в Краснодар", ("propose_order", "add_to_order")),
    ("про здоровье", "Можно ли пить улун при повышенном давлении?", None),
]


def consult_build():
    from app.modules.dialog.claude_client import _BASE_SYSTEM_PROMPT
    from app.modules.orders import conversation

    system = "\n\n".join([
        _BASE_SYSTEM_PROMPT, conversation._PII_PROMPT, f"Текущий ассортимент:\n{CATALOG}",
        conversation.order_flow_prompt(), conversation._describe_draft(None),
    ])
    return system, conversation._tools_for_stage(None)


async def request(client, model: str, effort: str, system: str, tools: list, messages: list):
    extra = {"output_config": {"effort": effort}} if effort else {}
    started = time.monotonic()
    response = await client.messages.create(
        model=model, max_tokens=8000, system=system, messages=messages, tools=tools, **extra,
    )
    return response, time.monotonic() - started


def summary(response) -> str:
    calls = [f"{b.name}({json.dumps(b.input, ensure_ascii=False)[:80]})" for b in response.content if b.type == "tool_use"]
    if calls:
        return "; ".join(calls)
    text = "".join(b.text for b in response.content if b.type == "text")
    return f"словами: {text[:110]!r}"


async def run_variant(model: str, effort: str, runs: int) -> dict:
    from anthropic import AsyncAnthropic

    from app import privacy
    from app.core.config import settings

    client = AsyncAnthropic(api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url or None)
    times, ok, total, refusals, out_tokens = [], 0, 0, 0, []
    print(f"\n=== {model} · effort={effort or 'по умолчанию'} ===")

    consult_system, consult_tools = consult_build()
    for title, said, expected in CONSULT_CASES:
        for _ in range(runs):
            messages = [{"role": "user", "content": said}]
            if privacy.scrub(consult_system + said)[1]:
                raise SystemExit("В запросе нашлись телефон или почта — не отправляю.")
            response, seconds = await request(client, model, effort, consult_system, consult_tools, messages)
            names = [b.name for b in response.content if b.type == "tool_use"]
            good = response.stop_reason != "refusal" and (
                (expected is None and not names) or (expected is not None and any(n in expected for n in names)) or
                (expected is None and response.stop_reason == "end_turn")
            )
            refusals += response.stop_reason == "refusal"
            ok += good
            total += 1
            times.append(seconds)
            out_tokens.append(response.usage.output_tokens)
            print(f"  {'ok ' if good else 'НЕТ'} {seconds:4.1f} с, вывод {response.usage.output_tokens:4d} · "
                  f"{title}: {response.stop_reason} · {summary(response)}")

    for title, point_chosen, before, said, needs_point in RECIPIENT_CASES:
        system, tools = build(point_chosen)
        for _ in range(runs):
            messages = [{"role": r, "content": c} for r, c in before] + [{"role": "user", "content": said}]
            response, seconds = await request(client, model, effort, system, tools, messages)
            calls = [(b.name, b.input) for b in response.content if b.type == "tool_use"]
            recipient = [a for n, a in calls if n == "set_recipient"]
            good = bool(recipient) and all(recipient[0].get(k) == v for k, v in LABELS.items())
            if needs_point:
                good = good and any(n == "set_delivery_method" and str(a.get("pickup_point")) == "1" for n, a in calls)
            refusals += response.stop_reason == "refusal"
            ok += good
            total += 1
            times.append(seconds)
            out_tokens.append(response.usage.output_tokens)
            print(f"  {'ok ' if good else 'НЕТ'} {seconds:4.1f} с, вывод {response.usage.output_tokens:4d} · "
                  f"{title}: {summary(response)}")

    times.sort()
    p95 = times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]
    result = {
        "variant": f"{model}:{effort}", "ok": f"{ok}/{total}", "refusals": refusals,
        "p50_s": round(statistics.median(times), 1), "p95_s": round(p95, 1), "max_s": round(times[-1], 1),
        "out_tokens_median": int(statistics.median(out_tokens)),
    }
    print("  итог:", result)
    return result


async def main(variants: list[str], runs: int) -> int:
    results = []
    for variant in variants:
        model, _, effort = variant.partition(":")
        results.append(await run_variant(model, effort, runs))
    print("\nСводка:")
    for r in results:
        print(f"  {r['variant']:28s} верно {r['ok']:>6s} · отказов {r['refusals']} · "
              f"p50 {r['p50_s']} с · p95 {r['p95_s']} с · max {r['max_s']} с · вывод ~{r['out_tokens_median']}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("variants", nargs="+", help="модель:effort, например claude-sonnet-5-5:low")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.variants, args.runs)))
