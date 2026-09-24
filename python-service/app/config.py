from functools import lru_cache
import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()


class Settings(BaseModel):
    api_key: str = Field(default="", alias="ANGEL_API_KEY")
    client_code: str = Field(default="", alias="ANGEL_CLIENT_CODE")
    pin: str = Field(default="", alias="ANGEL_PIN")
    totp_secret: str = Field(default="", alias="ANGEL_TOTP_SECRET")
    data_mode: str = Field(default="demo", alias="DATA_MODE")
    symbol: str = Field(default="NIFTY", alias="SYMBOL")
    exchange: str = Field(default="NFO", alias="EXCHANGE")
    token: str = Field(default="", alias="TOKEN")
    candle_interval_seconds: int = Field(default=60, alias="CANDLE_INTERVAL_SECONDS")
    port: int = Field(default=8000, alias="PYTHON_SERVICE_PORT")

    @property
    def credentials_ready(self) -> bool:
        return all((self.api_key, self.client_code, self.pin, self.totp_secret))


@lru_cache
def get_settings() -> Settings:
    return Settings(
        ANGEL_API_KEY=os.getenv("ANGEL_API_KEY", ""),
        ANGEL_CLIENT_CODE=os.getenv("ANGEL_CLIENT_CODE", ""),
        ANGEL_PIN=os.getenv("ANGEL_PIN", ""),
        ANGEL_TOTP_SECRET=os.getenv("ANGEL_TOTP_SECRET", ""),
        DATA_MODE=os.getenv("DATA_MODE", "demo"),
        SYMBOL=os.getenv("SYMBOL", "NIFTY"),
        EXCHANGE=os.getenv("EXCHANGE", "NFO"),
        TOKEN=os.getenv("TOKEN", ""),
        CANDLE_INTERVAL_SECONDS=int(os.getenv("CANDLE_INTERVAL_SECONDS", "60")),
        PYTHON_SERVICE_PORT=int(os.getenv("PYTHON_SERVICE_PORT", "8000")),
    )
