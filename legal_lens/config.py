"""Settings, read from the environment (and a .env file when present).

Nothing here has a default that points at a real service, so a missing
variable fails loudly instead of writing to the wrong database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    qdrant_url: str | None
    qdrant_api_key: str | None
    qdrant_collection: str
    neo4j_uri: str | None
    neo4j_username: str
    neo4j_password: str | None
    neo4j_database: str
    embedding_model: str
    embedding_dim: int
    database_url: str | None          # Supabase Postgres, optional for this package
    manifest_path: Path
    mappings_dir: Path

    def require(self, *names: str) -> None:
        missing = [n.upper() for n in names if not getattr(self, n)]
        if missing:
            raise SystemExit(
                "Missing settings: " + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in."
            )


def load_settings() -> Settings:
    _load_dotenv()
    env = os.environ.get
    return Settings(
        qdrant_url=env("QDRANT_URL"),
        qdrant_api_key=env("QDRANT_API_KEY"),
        qdrant_collection=env("QDRANT_COLLECTION", "judgment_chunks"),
        neo4j_uri=env("NEO4J_URI"),
        neo4j_username=env("NEO4J_USERNAME", "neo4j"),
        neo4j_password=env("NEO4J_PASSWORD"),
        neo4j_database=env("NEO4J_DATABASE", "neo4j"),
        embedding_model=env("EMBEDDING_MODEL", "BAAI/bge-m3"),
        embedding_dim=int(env("EMBEDDING_DIM", "1024")),
        database_url=env("DATABASE_URL"),
        manifest_path=Path(env("CASES_MANIFEST", str(ROOT / "data" / "cases.yaml"))),
        mappings_dir=Path(env("MAPPINGS_DIR", str(ROOT / "data" / "mappings"))),
    )
