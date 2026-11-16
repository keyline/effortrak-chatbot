from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')

    chat_mode: Literal['demo', 'openai'] = 'demo'
    openai_api_key: SecretStr = SecretStr('')
    openai_model: str = ''
    ai_token_metering_enabled: bool = False
    ai_help_llm_fallback_enabled: bool = False
    ai_help_followups_enabled: bool = False
    database_path: str = 'data/chat.sqlite3'
    application_log_path: str = 'logs/application.log'
    data_mode: Literal['sample', 'backend'] = 'sample'
    backend_url: str = ''
    ai_service_secret: SecretStr = SecretStr('')
    max_concurrent_chats: int = Field(default=8, ge=1, le=128)
    max_export_workers: int = Field(default=2, ge=1, le=8)
    report_ttl_seconds: int = Field(default=86400, ge=300, le=604800)
    conversation_database_url: SecretStr = SecretStr('')
    conversation_retention_days: int = Field(default=30, ge=1, le=365)
    max_company_concurrent_chats: int = Field(default=4, ge=1, le=32)
    background_export_rows: int = Field(default=200, ge=0, le=5000)
    run_export_workers: bool = True

    @model_validator(mode='after')
    def backend_configuration(self):
        storage = self.conversation_database_url.get_secret_value()
        if storage and not storage.startswith('postgresql+psycopg://'):
            raise ValueError('Shared conversation storage requires a postgresql+psycopg URL for a separate AI metadata database.')
        if self.data_mode == 'backend':
            if len(self.ai_service_secret.get_secret_value()) < 32:
                raise ValueError('Backend mode requires an AI_SERVICE_SECRET of at least 32 characters.')
            url = urlsplit(self.backend_url)
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('BACKEND_URL must be a configured HTTP(S) URL without credentials/query/fragment.')
            if url.scheme == 'http' and url.hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('Remote backend connections require HTTPS.')
        return self

    @property
    def database_file(self) -> Path:
        path = Path(self.database_path)
        return path if path.is_absolute() else ROOT / path

    @property
    def application_log_file(self) -> Path:
        path = Path(self.application_log_path)
        return path if path.is_absolute() else ROOT / path
