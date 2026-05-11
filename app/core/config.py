from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+psycopg2://vibeforge:vibeforge@db:5432/vibeforge"
    APP_ENV: str = "development"
    SECRET_KEY: str = "change-me-in-production"
    BASE_URL: str = "http://localhost:8000"  # Override in .env: BASE_URL=https://your-domain.com

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
