from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    GEMINI_API_KEY: str = "your_google_api_key_here"
    DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost:5432/procurement_db"
    QDRANT_URL: str = "http://localhost:6333"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
