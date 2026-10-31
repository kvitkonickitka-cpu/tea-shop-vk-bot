"""Small paid smoke check, synthetic data only; never prints credentials."""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main():
    from app.modules.dialog import openai_transport as transport

    summaries = []
    for model in ("gpt-6-luna", "gpt-6.1-sol"):
        messages = [{"role": "user", "content": "Найди цену тестового улуна инструментом catalog, затем сообщи цену."}]
        args = dict(model=model, messages=messages,
            system="Это синтетический тест. Цены только из catalog. Вызови catalog ровно один раз.",
            tools=[dict(name="catalog", description="Цена тестового чая", input_schema={
                "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})],
            max_tokens=1500, output_config={"effort": "low"})
        first = await transport.create(**args)
        uses = [b for b in first.content if b.type == "tool_use"]
        if len(uses) != 1 or uses[0].name != "catalog":
            raise RuntimeError("Smoke check: expected catalog function call")
        messages.extend([{"role": "assistant", "content": first.content}, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": uses[0].id, "content": '{"price_rub": 450}'}]}])
        second = await transport.create(**args, tool_choice={"type": "none"})
        text = " ".join(b.text for b in second.content if b.type == "text")
        if "450" not in text:
            raise RuntimeError("Smoke check: price missing from final answer")
        summaries.append(dict(model=model, status="passed", requests=2,
            input_tokens=first.usage.input_tokens + second.usage.input_tokens,
            output_tokens=first.usage.output_tokens + second.usage.output_tokens,
            cached_tokens=first.usage.cache_read_input_tokens + second.usage.cache_read_input_tokens))
    await transport.client().close()
    print(json.dumps(summaries, ensure_ascii=False))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # Error bodies may contain credential fragments. Only expose safe fields.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__,
                          "http_status": getattr(error, "status_code", None)}))
        sys.exit(1)
