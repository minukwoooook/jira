from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    oracle_dsn: str
    oracle_user: str
    oracle_password: str
    display_tz: str = "Asia/Seoul"
    pool_min: int = 2
    pool_max: int = 8
    call_timeout_ms: int = 30_000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
