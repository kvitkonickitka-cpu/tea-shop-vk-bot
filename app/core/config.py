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

    # Railway автоматически прокидывает DATABASE_URL при подключении Postgres
    database_url: str = ""

    # Тестовый (sandbox) контур CDEK по умолчанию — для продакшена сменить
    # на https://api.cdek.ru, когда появится боевой договор и ключи
    cdek_api_base_url: str = "https://api.edu.cdek.ru"
    cdek_client_id: str = ""
    cdek_client_secret: str = ""
    # Адрес, откуда забирают заказы (нужен для расчёта тарифа)
    cdek_from_address: str = ""
    # Заглушка веса заказа, пока нет точного веса по каждой упаковке
    cdek_default_package_weight_grams: int = 200


settings = Settings()
