"""
Alembic environment.

Two decisions worth knowing about:

  1. **The database URL comes from the environment, or from .env**, never from
     alembic.ini. The connection string contains a password, and a file that
     gets committed is the wrong place for one. A real environment variable
     wins over .env, which is what keeps Docker Compose authoritative there.

  2. **``compare_type`` and ``compare_server_default`` are on.** Without them,
     autogenerate silently misses a column changing from ``Integer`` to
     ``BigInteger`` -- which is exactly the sort of change this schema will
     need as the audit and push-item tables grow.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

# Make the application importable, so the models can be the source of truth.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Load .env, because a native deployment runs `alembic upgrade head` directly
# and nothing else puts DATABASE_URL into the environment for it.
#
# WHY THIS LINE EXISTS: under Docker Compose the URL is injected as a real
# environment variable, so this file worked there and only there. Run the same
# command on the Windows VPS -- which is what SETUP.md Step 7 tells an operator
# to do, and what scripts/deploy.ps1 does at step 5 of 6 -- and DATABASE_URL is
# simply absent, so every migration failed with "DATABASE_URL is not set". The
# first-time setup could not create its tables, and every later deployment
# carrying a migration would have stopped at the same point.
#
# load_dotenv does NOT overwrite variables that are already set, so Compose and
# any explicit `$env:DATABASE_URL=...` still take precedence over the file. The
# explicit requirement below is deliberately kept: reading the URL from
# app.config instead would pick up its fallback default and quietly migrate a
# DIFFERENT database than the operator intended.
load_dotenv(_REPO_ROOT / ".env")

from app.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The models define the target schema; autogenerate diffs against this.
target_metadata = Base.metadata


def _database_url() -> str:
    """The URL from the environment, with a helpful failure if it is absent."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set, so migrations cannot run.\n\n"
            "For example:\n"
            "  DATABASE_URL=postgresql+psycopg://autopilot:PASSWORD@localhost:5432/autopilot\n\n"
            "Inside Docker Compose this is set for you; run migrations with:\n"
            "  docker compose run --rm app alembic upgrade head"
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it.

    Useful when a client's DBA wants to review the statements before they are
    applied to a production database.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply the migrations."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # Keep every migration in one transaction where the backend allows
            # it, so a failure half way leaves no partial schema.
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
