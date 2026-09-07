"""
The migrations and the models must describe the same schema.

WHY THIS FILE EXISTS
====================
``migrations/versions/...initial_schema.py`` is frozen explicit DDL. That is the
correct way to write a migration -- a migration must say what the schema was at
that revision, not read whatever the models happen to say today -- but it
introduces a new failure mode: somebody edits ``app/models.py``, forgets to add
a migration, and the two drift apart.

That drift is invisible in development, because the test suite builds its
database with ``Base.metadata.create_all``. It shows up in production as a
column that does not exist, at the worst possible moment.

So this file applies the real migrations to an empty database and compares the
result, in detail, against what the models describe. If they differ, the test
fails and names the difference.

WHAT IS COMPARED
================
Tables, columns (name, type, nullability), primary keys, and indexes (name,
columns, uniqueness). Types are compared as the dialect renders them, which is
what actually matters -- ``VARCHAR(120)`` against ``VARCHAR(80)`` is a real
difference and this will catch it.

WHAT IS NOT, AND WHY
====================
These run on SQLite, so the PostgreSQL halves of the two dialect variants
(JSONB, BIGINT) are not exercised here. Both variants are declared once in
``app/models.py`` and reproduced verbatim in the migration, so a mismatch there
would be a copy error in a line the frozen file and the models share. The check
that matters -- that the *set* of tables, columns, keys and indexes agrees -- is
dialect-independent, and that is what is asserted.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from app.models import Base

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "versions"


def _revision_modules() -> list:
    """Import every migration module, in filename order."""
    modules = []
    for path in sorted(MIGRATIONS.glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"_mig_{path.stem}", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


def _fresh_engine():
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _schema(engine) -> dict:
    """
    A comparable description of everything in the database.

    Reflected rather than read from metadata, so this describes what the
    database actually contains.
    """
    insp = inspect(engine)
    out: dict = {}
    for table in sorted(insp.get_table_names()):
        if table == "alembic_version":
            continue  # bookkeeping, not part of the application schema
        out[table] = {
            "columns": {
                c["name"]: {
                    "type": str(c["type"]),
                    "nullable": bool(c["nullable"]),
                }
                for c in insp.get_columns(table)
            },
            "primary_key": tuple(insp.get_pk_constraint(table)["constrained_columns"]),
            "indexes": {
                ix["name"]: {
                    "columns": tuple(ix["column_names"]),
                    "unique": bool(ix["unique"]),
                }
                for ix in insp.get_indexes(table)
            },
        }
    return out


@pytest.fixture
def migrated_schema() -> dict:
    """The schema produced by running the migrations against an empty database."""
    engine = _fresh_engine()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            for module in _revision_modules():
                module.upgrade()
    schema = _schema(engine)
    engine.dispose()
    return schema


@pytest.fixture
def model_schema() -> dict:
    """The schema the models describe."""
    engine = _fresh_engine()
    Base.metadata.create_all(engine)
    schema = _schema(engine)
    engine.dispose()
    return schema


# ---------------------------------------------------------------------------
# The drift check itself
# ---------------------------------------------------------------------------

def test_migrations_create_every_table_the_models_declare(migrated_schema, model_schema):
    """No table is missing from the migrations, and none is left over."""
    missing = sorted(set(model_schema) - set(migrated_schema))
    extra = sorted(set(migrated_schema) - set(model_schema))
    assert not missing, (
        f"the models declare tables the migrations never create: {missing}. "
        "Add a migration."
    )
    assert not extra, (
        f"the migrations create tables the models no longer declare: {extra}. "
        "Either the model was deleted without a migration, or the migration is wrong."
    )


def test_migrated_columns_match_the_models(migrated_schema, model_schema):
    """Every column, its type and its nullability agree."""
    problems: list[str] = []
    for table in sorted(set(model_schema) & set(migrated_schema)):
        want = model_schema[table]["columns"]
        got = migrated_schema[table]["columns"]

        for name in sorted(set(want) - set(got)):
            problems.append(f"{table}.{name}: in the models, missing from the migrations")
        for name in sorted(set(got) - set(want)):
            problems.append(f"{table}.{name}: created by the migrations, not in the models")
        for name in sorted(set(want) & set(got)):
            if want[name] != got[name]:
                problems.append(f"{table}.{name}: models {want[name]} != migrations {got[name]}")

    assert not problems, "schema drift between app/models.py and migrations/:\n  " + "\n  ".join(
        problems
    )


def test_migrated_keys_and_indexes_match_the_models(migrated_schema, model_schema):
    """
    Primary keys and indexes agree.

    Indexes are not cosmetic here. ``ix_listing_barcode_prefix`` is what makes
    matching 45,511 listings against a 1.15-million-row feed finish in seconds
    rather than minutes, and a missing index shows up only as a system that has
    quietly become too slow to keep up with a five-minute feed.
    """
    problems: list[str] = []
    for table in sorted(set(model_schema) & set(migrated_schema)):
        if model_schema[table]["primary_key"] != migrated_schema[table]["primary_key"]:
            problems.append(
                f"{table}: primary key {model_schema[table]['primary_key']} "
                f"!= {migrated_schema[table]['primary_key']}"
            )
        want = model_schema[table]["indexes"]
        got = migrated_schema[table]["indexes"]
        for name in sorted(set(want) - set(got)):
            problems.append(f"{table}: index {name} is in the models but not the migrations")
        for name in sorted(set(got) - set(want)):
            problems.append(f"{table}: index {name} is in the migrations but not the models")
        for name in sorted(set(want) & set(got)):
            if want[name] != got[name]:
                problems.append(
                    f"{table}: index {name} differs -- models {want[name]} "
                    f"!= migrations {got[name]}"
                )

    assert not problems, "index/key drift between app/models.py and migrations/:\n  " + "\n  ".join(
        problems
    )


# ---------------------------------------------------------------------------
# The baseline must not read the models
# ---------------------------------------------------------------------------

def test_the_baseline_does_not_build_itself_from_the_models():
    """
    The initial revision must be frozen DDL, not ``create_all``.

    Guarding this with a test rather than a comment, because the shortcut is
    tempting and its consequence -- a fresh install that fails while an existing
    one succeeds -- appears months later during a rebuild.
    """
    baseline = next(iter(sorted(MIGRATIONS.glob("*.py"))))
    source = baseline.read_text(encoding="utf-8")
    # A *call* -- the docstring names the anti-pattern in prose deliberately, so
    # anyone tempted to reintroduce it reads why not first.
    assert "metadata.create_all(" not in source, (
        f"{baseline.name} builds the schema from the live models. A migration must "
        "describe the schema as it was AT that revision; one that reads today's "
        "models breaks the first time a second revision exists."
    )
    assert source.count("op.create_table") >= 17, (
        f"{baseline.name} should create every table explicitly"
    )


def test_downgrading_the_baseline_refuses_without_an_explicit_opt_in(monkeypatch):
    """
    ``alembic downgrade`` on the initial revision must refuse by default.

    It drops every table, including the undo trail. The deploy script used to suggest
    exactly this command after a failed release.
    """
    monkeypatch.delenv("ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE", raising=False)
    baseline = _revision_modules()[0]

    engine = _fresh_engine()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            baseline.upgrade()
            with pytest.raises(RuntimeError, match="Refusing to downgrade"):
                baseline.downgrade()

    # Nothing was dropped by the refusal.
    assert "push_items" in inspect(engine).get_table_names()
    engine.dispose()


def test_downgrading_the_baseline_works_with_the_opt_in(monkeypatch):
    """The escape hatch has to actually work, or someone will delete the guard."""
    baseline = _revision_modules()[0]
    monkeypatch.setenv(
        "ALEMBIC_ALLOW_DESTRUCTIVE_DOWNGRADE", baseline.DESTRUCTIVE_OPT_IN
    )

    engine = _fresh_engine()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            baseline.upgrade()
            assert "push_items" in inspect(conn).get_table_names()
            baseline.downgrade()

    assert inspect(engine).get_table_names() == []
    engine.dispose()
