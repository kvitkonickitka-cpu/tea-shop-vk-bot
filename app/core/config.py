from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "Tea Shop VK Bot Backend"
    debug: bool = False

    vk_confirmation_token: str = ""
    vk_secret_key: str = ""
    vk_group_id: str = ""
    vk_access_token: str = ""
    vk_market_user_token: str = ""
    vk_api_version: str = "5.199"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"


settings = Settings()
