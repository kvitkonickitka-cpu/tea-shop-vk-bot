"""Контакты получателя: телефон к одному виду, почта — живая.

Почта здесь критична: «Чеки от ЮKassa» доставляют чек **только письмом**.
Адрес с опечаткой ЮKassa примет — синтаксис у него верный, — и чек уйдёт в
никуда: клиент его не получит, а мы об этом не узнаем. Поэтому мало, чтобы
адрес был похож на адрес. У домена должен быть почтовый сервер (MX-запись):
`yandex.ry`, `gmial.com`, `test.ru` его не имеют. Для опечатки в известном
домене подсказываем правильный.

Телефон храним в одном виде, `+7XXXXXXXXXX`: клиент пишет как привык —
через восьмёрку, со скобками, без кода страны, — а перевозчики и менеджер
должны видеть одно и то же. Посылки идут по России, получатель ждёт СМС от
СДЭКа или Ozon, поэтому номер нужен российский.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import dns.resolver

logger = logging.getLogger(__name__)


def normalize_phone(raw: str) -> str | None:
    """`+7XXXXXXXXXX` или None, если это не российский номер."""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return None


# Латиница в имени ящика: кириллицу там ЮKassa и почтовые серверы
# принимают не везде. Домен может быть и кириллическим (`почта.рф`) — его
# переводим в punycode перед запросом DNS.
_LOCAL = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*$")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")

# Самые частые почтовые домены клиентов — для подсказки при опечатке.
_KNOWN_DOMAINS = (
    "yandex.ru", "ya.ru", "mail.ru", "bk.ru", "inbox.ru", "list.ru",
    "internet.ru", "rambler.ru", "gmail.com", "icloud.com", "outlook.com",
    "hotmail.com", "yahoo.com", "proton.me",
)

# Сколько ждать DNS. Проверка идёт внутри хода диалога, а у вебхука ВК на
# всё около восьми секунд.
_DNS_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True)
class EmailCheck:
    ok: bool
    email: str = ""
    # Почему не годится — для модели, она перескажет клиенту своими словами.
    problem: str = ""
    # Вероятно имелось в виду — при опечатке в известном домене.
    suggestion: str = ""


def _split(raw: str) -> tuple[str, str] | None:
    # Скобки и кавычки вокруг и точку в конце фразы клиент ставит часто;
    # точка в начале — уже часть (неверного) адреса.
    email = (raw or "").strip().strip("()<>\"'").rstrip(".,;:")
    if email.count("@") != 1:
        return None
    local, domain = email.split("@")
    domain = domain.lower().rstrip(".")
    if not local or len(local) > 64 or not _LOCAL.match(local):
        return None
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    labels = ascii_domain.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        return None
    # Зона верхнего уровня — буквы (или punycode `xn--`), не цифры.
    if not (labels[-1].isalpha() and len(labels[-1]) >= 2 or labels[-1].startswith("xn--")):
        return None
    return local, ascii_domain


def _suggest(local: str, domain: str) -> str:
    match = difflib.get_close_matches(domain, _KNOWN_DOMAINS, n=1, cutoff=0.75)
    return f"{local}@{match[0]}" if match and match[0] != domain else ""


async def _has_mail_server(domain: str) -> bool | None:
    """Принимает ли домен почту. None — DNS не ответил, судить не можем."""
    try:
        answer = await dns.asyncresolver.resolve(domain, "MX", lifetime=_DNS_TIMEOUT_SECONDS)
    except dns.resolver.NXDOMAIN:
        return False
    except dns.resolver.NoAnswer:
        # Домен есть, почтового сервера нет. По RFC 5321 почту можно слать и
        # на адрес самого домена, но у ящиков покупателей так не бывает, а
        # у опечаток вроде `gmial.com` — сплошь и рядом.
        return False
    except (dns.exception.Timeout, dns.resolver.NoNameservers, OSError) as error:
        logger.warning("Почту «@%s» не проверили: DNS не ответил — %s", domain, error)
        return None
    # «Нулевой MX» (RFC 7505, запись «.») — домен прямо говорит, что почту
    # не принимает. Так устроены example.com и ему подобные.
    return any(str(record.exchange) not in (".", "") for record in answer)


async def check_email(raw: str) -> EmailCheck:
    parts = _split(raw)
    if parts is None:
        return EmailCheck(
            ok=False,
            problem=f"«{(raw or '').strip()}» не похоже на адрес почты: нужен вид name@example.ru",
        )
    local, domain = parts
    email = f"{local}@{domain}"

    has_mail = await _has_mail_server(domain)
    if has_mail is False:
        return EmailCheck(
            ok=False,
            email=email,
            problem=f"домен «{domain}» не принимает почту — письмо с чеком не дойдёт",
            suggestion=_suggest(local, domain),
        )
    # DNS молчит — не держим клиента из-за своей сети: синтаксис верный,
    # а отказать живому адресу хуже, чем пропустить непроверенный.
    return EmailCheck(ok=True, email=email)
