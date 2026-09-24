"""Сборка PDF «Что происходит на стороне клиента» из index.html.

    python docs/client-flow/render.py

Нужны playwright и Chromium. Если браузер лежит не там, где его ищет
playwright (так бывает в контейнерах с предустановленным Chromium),
путь передаётся переменной CHROME_PATH.
"""

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

D = str(Path(__file__).resolve().parent)
OUT = str(Path(__file__).resolve().parents[1] / "client-flow.pdf")


async def main():
    async with async_playwright() as p:
        launch = {"args": ["--no-sandbox"]}
        chrome = os.environ.get("CHROME_PATH")
        if chrome:
            launch["executable_path"] = chrome
        browser = await p.chromium.launch(**launch)
        page = await browser.new_page()
        await page.goto(f"file://{D}/index.html", wait_until="networkidle")
        await page.pdf(
            path=OUT,
            format="A4",
            print_background=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="width:100%;font-size:8px;color:#8b948d;'
                'font-family:DejaVu Sans;padding:0 14mm;display:flex;'
                'justify-content:space-between;">'
                '<span>tea-shop-vk-bot · флоу глазами клиента</span>'
                '<span class="pageNumber"></span></div>'
            ),
            margin={"top": "14mm", "bottom": "16mm", "left": "14mm", "right": "14mm"},
        )
        await browser.close()
    print("готово:", OUT)


asyncio.run(main())
