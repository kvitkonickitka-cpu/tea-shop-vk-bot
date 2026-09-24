"""Вложения сообщения ВКонтакте: что из них можно показать Claude.

Раньше обработчик выходил на первой же строке, если в сообщении не было
текста: клиент присылал фотографию — чая, скриншот с адресом пункта выдачи,
снимок оплаты — и не получал ничего, даже «я это не вижу». Молчание бота на
фото выглядит как поломка, и по сути ею и было.

Фотографии скачиваем сами и отдаём модели содержимым, а не ссылкой. Ссылку
Anthropic должен был бы сходить забрать у VK — лишняя чужая сеть в середине
хода, про которую в наших логах не будет ни строки. Всё остальное (голосовые,
видео, документы, стикеры) показать нельзя, поэтому про них просто говорим
словами: модель ответит, что посмотреть не может, и попросит написать текстом.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# Сколько фотографий берём из одного сообщения. ВК разрешает десять, но
# каждая — это и секунды на скачивание, и токены в запросе.
_MAX_IMAGES = 2
# Потолок на снимок. У Anthropic предел 5 МБ на изображение, и до него мы
# доходить не хотим: фото из ВК заметно меньше, а всё, что больше, скорее
# всего не фотография.
_MAX_BYTES = 3 * 1024 * 1024
# Ширина, которой хватает, чтобы прочитать надпись на упаковке. Брать
# оригинал незачем: он тяжелее в разы, а разглядеть по нему больше нечего.
_PREFERRED_WIDTH = 1280
_TIMEOUT_SECONDS = 4

# Что за вложение пришло — словами для модели.
_KIND_NOTES = {
    "audio_message": "голосовое сообщение",
    "video": "видео",
    "doc": "файл",
    "sticker": "стикер",
    "audio": "аудиозапись",
    "link": "ссылку",
    "market": "товар",
    "wall": "запись со стены",
    "graffiti": "граффити",
}


@dataclass
class Collected:
    """Что удалось достать из вложений сообщения."""

    # Блоки изображений в том виде, в каком их принимает Anthropic.
    images: list[dict] = field(default_factory=list)
    # Чего показать не смогли — словами, для системной подсказки.
    notes: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.images or self.notes)


def _best_size(photo: dict) -> str:
    """Ссылка на подходящий размер фотографии."""
    sizes = [s for s in (photo.get("sizes") or []) if s.get("url")]
    if not sizes:
        return ""
    fitting = [s for s in sizes if (s.get("width") or 0) <= _PREFERRED_WIDTH]
    # Из подходящих берём самый крупный, а если все крупнее нужного — самый
    # мелкий из них: и то и другое лучше, чем наугад первый в списке.
    chosen = max(fitting, key=lambda s: s.get("width") or 0) if fitting else min(
        sizes, key=lambda s: s.get("width") or 0
    )
    return chosen.get("url") or ""


async def _download(client: httpx.AsyncClient, url: str) -> dict | None:
    response = await client.get(url)
    response.raise_for_status()
    content = response.content
    if len(content) > _MAX_BYTES:
        logger.info("Фотография из ВК велика (%s байт), пропускаем", len(content))
        return None
    media_type = (response.headers.get("content-type") or "image/jpeg").split(";")[0]
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        logger.info("Вложение из ВК не картинка (%s), пропускаем", media_type)
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.b64encode(content).decode("ascii"),
        },
    }


async def collect(message: dict) -> Collected:
    """Разобрать вложения сообщения: картинки скачать, остальное назвать."""
    collected = Collected()
    attachments = message.get("attachments") or []
    if not attachments:
        return collected

    urls: list[str] = []
    for attachment in attachments:
        kind = attachment.get("type") or ""
        if kind == "photo":
            url = _best_size(attachment.get("photo") or {})
            if url and len(urls) < _MAX_IMAGES:
                urls.append(url)
                continue
        note = _KIND_NOTES.get(kind)
        if note:
            collected.notes.append(note)
        elif kind != "photo":
            collected.notes.append(f"вложение «{kind}»")

    if not urls:
        return collected

    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=True) as client:
        for url in urls:
            try:
                image = await _download(client, url)
            except Exception:
                logger.exception("Не скачали фотографию клиента из ВК")
                image = None
            if image is None:
                # Клиенту всё равно нужно объяснение, почему бот не видит
                # присланного, — иначе он повторит то же самое ещё раз.
                collected.notes.append("фотографию, которую не удалось открыть")
            else:
                collected.images.append(image)

    return collected


def describe(collected: Collected) -> str:
    """Чем дополнить текст клиента, когда показать вложение нельзя."""
    if not collected.notes:
        return ""
    return "Клиент прислал " + ", ".join(collected.notes) + "."
