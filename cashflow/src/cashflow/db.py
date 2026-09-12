"""Подключение к PostgreSQL и применение миграций."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# 001 создаёт роли и схему, её применяет суперпользователь вручную.
SUPERUSER_ONLY = {"001_schema_and_roles"}


def connect(dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(dsn, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("SET search_path TO finance, pg_catalog")
    return conn


def applied_versions(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'finance' AND table_name = 'schema_migrations')"
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return set()
        cur.execute("SELECT version FROM finance.schema_migrations")
        return {r[0] for r in cur.fetchall()}


def pending_migrations(conn: psycopg.Connection) -> list[Path]:
    done = applied_versions(conn)
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [f for f in files if f.stem not in done and f.stem not in SUPERUSER_ONLY]


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Применяет непринятые миграции по порядку. Каждая — в своей транзакции."""
    if not applied_versions(conn):
        raise RuntimeError(
            "В базе нет таблицы finance.schema_migrations. Сначала примените "
            "migrations/001_schema_and_roles.sql от имени суперпользователя — "
            "она создаёт схему и роли. Подробности в docs/deploy.md."
        )

    applied: list[str] = []
    for path in pending_migrations(conn):
        sql = path.read_text(encoding="utf-8")
        # psql-директивы вроде \set psycopg не понимает.
        sql = re.sub(r"^\s*\\[a-z_]+.*$", "", sql, flags=re.MULTILINE)
        log.info("Применяю миграцию %s", path.stem)
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        applied.append(path.stem)
    return applied
