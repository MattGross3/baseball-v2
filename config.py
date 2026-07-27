"""Application settings, read from the environment and an optional .env file.

Deliberately small in Phase 0: there are no external API keys yet because
there are no ingest workers yet (see the plan's "Deliberately NOT in this
phase"). The only thing that needs configuring is where Postgres lives.
"""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Port 5433, not 5432: a native PostgreSQL 18 install on this machine
    # holds the v1 project's live database on 5432. Defaulting there would
    # mean an unconfigured checkout migrates over real data.
    database_url: str = Field(
        default="postgresql+psycopg://baseball:baseball@localhost:5433/baseball",
    )
    test_database_url: str = Field(
        default="postgresql+psycopg://baseball:baseball@localhost:5433/baseball_test",
    )
    sql_echo: bool = False

    # Connection pool. Carried over from the v1 project, which arrived at these
    # numbers after a long-running backtest held a connection open and made the
    # whole app look hung: a bounded pool with a short timeout turns that into
    # a fast, legible error instead of an indefinite wait.
    pool_size: int = 5
    pool_max_overflow: int = 10
    pool_timeout_seconds: int = 15

    @property
    def sync_database_url(self) -> str:
        """The same URL, for Alembic's synchronous migration runner.

        psycopg3 drives both modes, so this is the identical string - the
        property exists to make the two call sites self-documenting and to
        give a single place to diverge if the driver ever changes.
        """
        return self.database_url


settings = Settings()
