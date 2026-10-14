from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')

    chat_mode: Literal['demo', 'openai'] = 'demo'
    openai_api_key: SecretStr = SecretStr('')
    openai_model: str = ''
    semantic_questions: bool = False
    database_path: str = 'data/chat.sqlite3'
    data_mode: Literal['sample', 'backend'] = 'sample'
    backend_url: str = ''
    ai_service_secret: SecretStr = SecretStr('')

    @model_validator(mode='after')
    def backend_configuration(self):
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
