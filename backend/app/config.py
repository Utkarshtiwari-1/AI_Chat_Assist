from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "llama3.2:3b"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    chroma_persist_dir: str = "./data/chroma"
    data_dir: str = "./data"
    crawler_max_pages: int = 500
    crawler_max_depth: int = 5
    crawler_seed_urls: str = "https://www.nitrr.ac.in/"
    crawler_max_file_size: int = 20_000_000
    request_timeout: int = 15
    retrieval_top_k: int = 5
    relevance_threshold: float = 0.35
    chunk_size: int = 900
    chunk_overlap: int = 180
    frontend_origin: str = "http://127.0.0.1:5173"

    @property
    def root_dir(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else self.root_dir / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
