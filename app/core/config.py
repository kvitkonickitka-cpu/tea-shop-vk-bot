from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "Tea Shop VK Bot Backend"
    debug: bool = False

    vk_confirmation_token: str = ""
    vk_secret_key: str = ""
    vk_group_id: str = ""
    vk_access_token: str = ""
    vk_api_version: str = "5.199"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    # Прокси перед api.anthropic.com — нужен, когда хостинг физически в РФ
    # и Anthropic блокирует прямые запросы оттуда. Пусто = обращаться напрямую.
    anthropic_base_url: str = ""

    # Railway автоматически прокидывает DATABASE_URL при подключении Postgres
    database_url: str = ""

    # Боевой контур СДЭК. Песочница (https://api.edu.cdek.ru) живёт на
    # отдельных ключах, которые выдаёт менеджер: те, что приходят письмом
    # при регистрации личного кабинета, там не работают. Расчёт тарифа —
    # операция чтения, поэтому проверять его на боевом безопасно.
    cdek_api_base_url: str = "https://api.cdek.ru"
    cdek_client_id: str = ""
    cdek_client_secret: str = ""
    # Адрес, откуда забирают заказы (нужен для расчёта тарифа)
    cdek_from_address: str = ""
    # Код отделения СДЭК, куда сами сдаём посылки. Без него заказ по тарифу
    # «от склада» не зарегистрировать. Посмотреть коды по своему городу —
    # scripts/cdek_points_probe.py
    cdek_shipment_point: str = ""
    # Заглушка веса заказа, пока нет точного веса по каждой упаковке
    cdek_default_package_weight_grams: int = 200

    # Telegram-бот для уведомлений менеджера об эскалациях из чата с клиентом
    telegram_bot_token: str = ""
    telegram_manager_chat_id: str = ""
    # Прокси перед api.telegram.org — как и у Anthropic, нужен потому, что из
    # российского дата-центра Telegram недоступен: проверено с виртуалки,
    # соединение просто висит до таймаута. Пусто = обращаться напрямую.
    telegram_api_base_url: str = ""
    # Секрет в заголовке X-Proxy-Secret, которым прокси отличает наши запросы
    # от чужих — без него /tg/* был бы открытым релеем в Telegram для всех.
    # Используется только вместе с telegram_api_base_url.
    telegram_proxy_secret: str = ""
    # Отдельный чат для мини-отчётов по завершённым диалогам. Пусто = отчёты
    # не отправляются вовсе.
    telegram_reports_chat_id: str = ""
    # Сколько диалог должен молчать, чтобы считаться завершённым.
    dialog_report_idle_minutes: int = 20
    # За один запуск по таймеру — не больше стольких отчётов, чтобы уложиться
    # в отведённое контейнеру время выполнения.
    dialog_report_batch_limit: int = 10
    # Общий секрет для служебных эндпоинтов (их дёргает таймер). Адрес
    # контейнера открыт всему интернету, так что без этого отчёты сможет
    # запускать кто угодно. Пусто = служебные эндпоинты закрыты полностью.
    internal_api_token: str = ""


settings = Settings()
