"""The data layer: how the systems a user connects become one queryable thing.

Two backends:
  direct   every connected database is its own source (what runs today, and the fallback for development)
  zetaris  Overwatch registers each connected database as a Zetaris datasource and agents query only Zetaris,
           so one query can join tables from different systems (deploys in one database, orders in another)

The end user never configures Zetaris: they add their databases in the setup page; Overwatch registers them.
The Zetaris endpoint itself comes from the deployment's environment (ZETARIS_JDBC_URL ...).
"""

from __future__ import annotations

import os
import re
from typing import Any

from sqlalchemy.engine import make_url

from .sources.zetaris import ZetarisSource

JDBC_DRIVERS = {"postgresql": "org.postgresql.Driver", "mysql": "com.mysql.cj.jdbc.Driver",
                "mariadb": "org.mariadb.jdbc.Driver", "mssql": "com.microsoft.sqlserver.jdbc.SQLServerDriver"}


def zetaris_env() -> dict[str, str]:
    """The deployment's Zetaris connection, or {} when this install does not use Zetaris."""
    cfg = {"jdbc_url": os.environ.get("ZETARIS_JDBC_URL", ""), "driver_class": os.environ.get("ZETARIS_DRIVER_CLASS", ""),
           "driver_jar": os.environ.get("ZETARIS_DRIVER_JAR", ""), "username": os.environ.get("ZETARIS_USER", ""),
           "password": os.environ.get("ZETARIS_PASSWORD", "")}
    return cfg if cfg["jdbc_url"] else {}


def datasource_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "source"


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_jdbc(sqlalchemy_url: str) -> dict[str, str]:
    """postgresql+psycopg://u:p@host:5432/db  ->  jdbc url, driver class, user, password."""
    url = make_url(sqlalchemy_url)
    backend = url.get_backend_name()
    if backend not in JDBC_DRIVERS:
        raise ValueError(f"cannot register a '{backend}' database with Zetaris yet (supported: {', '.join(JDBC_DRIVERS)})")
    port = f":{url.port}" if url.port else ""
    return {"jdbcurl": f"jdbc:{backend}://{url.host}{port}/{url.database or ''}", "jdbcdriver": JDBC_DRIVERS[backend],
            "username": url.username or "", "password": url.password or ""}


def registration_statements(name: str, sqlalchemy_url: str, description: str = "") -> list[str]:
    """The Zetaris SQL that makes a user's database queryable as <name>.<table>."""
    ds = datasource_name(name)
    jdbc = to_jdbc(sqlalchemy_url)
    options = ", ".join(f"{k} {_q(v)}" for k, v in {**jdbc, "fetchsize": "1000"}.items())
    return [
        f"CREATE DATASOURCE {ds} DESCRIBE BY {_q(description or 'connected by Overwatch')} OPTIONS ({options})",
        f"REGISTER DATASOURCE TABLES FROM {ds}",
    ]


def register(databases: dict[str, str], zetaris: dict[str, Any] | None = None) -> list[str]:
    """Register every database with the deployment's Zetaris. Returns what was run."""
    cfg = zetaris or zetaris_env()
    if not cfg:
        raise RuntimeError("Zetaris is not configured for this deployment (ZETARIS_JDBC_URL is not set)")
    src = ZetarisSource("zetaris", cfg)
    try:
        done: list[str] = []
        for name, url in databases.items():
            done += src.admin(registration_statements(name, url))
        return done
    finally:
        src.close()


def source_config() -> dict[str, str]:
    """The `zetaris` source block for overwatch.yaml: references only, secrets stay in .env."""
    return {"type": "zetaris", "jdbc_url": "${ZETARIS_JDBC_URL}", "driver_class": "${ZETARIS_DRIVER_CLASS}",
            "driver_jar": "${ZETARIS_DRIVER_JAR}", "username": "${ZETARIS_USER}", "password": "${ZETARIS_PASSWORD}"}


__all__ = ["register", "registration_statements", "to_jdbc", "zetaris_env", "source_config", "datasource_name"]
