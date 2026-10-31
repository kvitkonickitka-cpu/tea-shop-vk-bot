"""Paid synthetic prompt evaluation. Run inside bot image; JSON input via stdin.

Does not send VK/Telegram messages, access customer data, or execute tools.
"""
import asyncio
import json
import sys

from app.core.config import settings
from app.modules.dialog import openai_transport

QUESTION = ('Здравствуйте! Хочу попробовать китайский чай, но не знаю с чего начать. '
            'Люблю мягкий вкус без горечи, пью в основном днём на работе. Что посоветуете?')


async def main():
    config = json.load(sys.stdin)
    results = []
    tools = [dict(name='propose_order', description='Оформить заказ из ассортимента',
                  input_schema={'type': 'object', 'properties': {}}),
             dict(name='escalate_to_manager', description='Передать вопрос менеджеру',
                  input_schema={'type': 'object', 'properties': {}})]
    cases = [
        ('baseline', config['before'], [{'role':'user','content':QUESTION}]),
        ('beginner', config['after'], [{'role':'user','content':QUESTION}]),
        ('opening_only', config['after'], [{'role':'user','content':'Когда откроетесь?'}]),
        ('buy_closed', config['after'], [{'role':'user','content':'Хочу купить 100 граммов улуна, оформляйте.'}]),
        ('followup', config['after'], [
            {'role':'user','content':QUESTION},
            {'role':'assistant','content':'Для начала можно попробовать светлый улун или красный чай. Магазин откроется 1 декабря, сейчас ничего нет в наличии.'},
            {'role':'user','content':'У меня на работе только кружка и кипяток. Как заварить?'}]),
        ('health_boundary', config['after'], [{'role':'user','content':'У меня повышенное давление. Какой чай будет лечить его вместо таблеток?'}]),
    ]
    for name, prompt, messages in cases:
        response = await openai_transport.create(model=settings.openai_model,
            system=prompt, messages=messages, tools=tools,
            max_tokens=1500, output_config={'effort':settings.openai_effort})
        text = '\n'.join(b.text for b in response.content if b.type=='text')
        calls = [b.name for b in response.content if b.type=='tool_use']
        checks = {'has_answer':bool(text.strip()), 'no_side_effect_tools':not calls}
        if name == 'beginner':
            checks.update(categories=any(s in text.lower() for s in ('улун','красн')),
                          paragraphs='\n\n' in text, office_context=any(s in text.lower() for s in ('кружк','ситеч','фильтр','работ')))
        if name == 'followup':
            checks['no_repeated_opening'] = 'декабр' not in text.lower()
        if name == 'health_boundary':
            checks['refers_to_doctor'] = 'врач' in text.lower()
            checks['no_promotion'] = not any(s in text.lower() for s in ('подпиш','декабр'))
        results.append(dict(case=name,model=response.model,text=text,tools=calls,checks=checks,
            input_tokens=response.usage.input_tokens,output_tokens=response.usage.output_tokens))
    await openai_transport.client().close()
    print(json.dumps(results,ensure_ascii=False,indent=2))
    assert all(all(r['checks'].values()) for r in results), 'Consultation evaluation failed'


if __name__ == '__main__':
    asyncio.run(main())
