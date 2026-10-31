from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnalyticsBotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ANALYTICS_BOT_", extra="ignore")
    token: SecretStr
    chat_id: int
    telegram_base_url: str = "https://api.telegram.org"
    proxy_secret: SecretStr = SecretStr("")
    sales_database_url: SecretStr = SecretStr("")
    openai_api_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    openai_proxy_secret: SecretStr = SecretStr("")
    openai_model: str = "gpt-6.1-sol"
    daily_question_limit: int = Field(default=50, ge=1, le=1000)
    daily_enabled: bool = False
    daily_hour_msk: int = Field(default=9, ge=0, le=23)
    state_path: Path = Path("/data/analytics-bot.sqlite3")

    @model_validator(mode="after")
    def validate_secrets(self):
        from urllib.parse import urlsplit
        import os

        url = urlsplit(self.telegram_base_url)
        if url.scheme != "https" or not url.hostname or url.username or url.query or url.fragment:
            raise ValueError("Telegram base must be an HTTPS URL without credentials/query")
        if not self.token.get_secret_value().strip():
            raise ValueError("A dedicated Telegram token is required")
        if self.token.get_secret_value() == os.getenv("TELEGRAM_BOT_TOKEN"):
            raise ValueError("Analytics must not reuse the operational bot token")
        if self.chat_id >= 0:
            raise ValueError("Configure a group chat, not private messages")
        api = urlsplit(self.openai_base_url)
        if api.scheme != "https" or not api.hostname or api.username or api.query or api.fragment:
            raise ValueError("OpenAI base must be an HTTPS URL")
        return self
