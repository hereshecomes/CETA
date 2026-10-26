"""Run Weblate in-process against a per-world PostgreSQL database (user-space pgserver).

All application behaviour (permission checks, API views, audit logging, the periodic
disable-expired task) is Weblate's own code. The harness only (a) provides the database and
data directory, (b) controls the clock with time-machine, and (c) drives HTTP requests through
Django's test client (WSGI handler).

Database layout: one pgserver cluster (PG_ROOT) hosts a migrated template database
(TEMPLATE_DB) and one database per world, created with CREATE DATABASE ... TEMPLATE ... .
"""
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
# Data directory for the application environments, sources and executed worlds (see README):
# $CETA_DATA, default <repository>/data.
DATA = Path(os.environ.get("CETA_DATA", str(HERE.parents[1] / "data")))
LOCAL = Path(os.environ.get("CETA_TMP", "/tmp/ceta")) / "wl"  # container-local disk: fast, regenerable (cluster + working data dirs)
PG_ROOT = LOCAL / "pg_cluster"
WORK_ROOT = LOCAL / "worlds"
WORLDS_ROOT = DATA / "worlds" / "weblate"  # executed worlds: ledgers and state dumps
TEMPLATE_DB = "wl_template"
PY = DATA / "envs" / "weblate" / "bin" / "python"


def start_cluster(pgdata=PG_ROOT):
    """Start (or attach to) the pgserver cluster; returns (server, socket_dir)."""
    import pgserver

    pgdata = Path(pgdata)
    pgdata.parent.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(pgdata), cleanup_mode=None)
    uri = server.get_uri()
    q = parse_qs(urlparse(uri).query)
    host = q.get("host", [""])[0]
    port = q.get("port", [""])[0]
    if not host:
        host = urlparse(uri).hostname or "127.0.0.1"
        port = port or str(urlparse(uri).port or "")
    return server, host, port


def db_exists(server, name):
    out = server.psql(f"SELECT 1 FROM pg_database WHERE datname = '{name}';")
    return "1 row" in out or "(1 row)" in out


def create_db(server, name, template=None):
    if db_exists(server, name):
        server.psql(f"DROP DATABASE {name};")
    if template:
        server.psql(f"CREATE DATABASE {name} TEMPLATE {template};")
    else:
        server.psql(f"CREATE DATABASE {name};")


def drop_db(server, name):
    if db_exists(server, name):
        server.psql(f"DROP DATABASE {name};")


def configure(db_name, host, port, data_dir):
    """Point Weblate at a database and data directory, then django.setup()."""
    data_dir = Path(data_dir)
    if data_dir.exists():
        shutil.rmtree(data_dir)
    for sub in ("vcs", "cache", "media", "home", "backups", "ssh"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    os.environ["WL_DB_NAME"] = db_name
    os.environ["WL_DB_HOST"] = host
    os.environ["WL_DB_PORT"] = port or ""
    os.environ["WL_DATA_DIR"] = str(data_dir)
    os.environ["DJANGO_SETTINGS_MODULE"] = "wl_settings"
    os.environ.setdefault("HOME", str(data_dir / "home"))
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import django

    django.setup()
    # Weblate's own git identity setup (what its startup does in production): writes DATA_DIR/home/.gitconfig
    from weblate.vcs.git import GitRepository

    GitRepository.global_setup()


def migrate_template(server, host, port, data_dir):
    """Create and migrate the template database in THIS process (call from a dedicated process:
    PostgreSQL refuses to clone a template while connections to it are open)."""
    create_db(server, TEMPLATE_DB)
    configure(TEMPLATE_DB, host, port, data_dir)
    from django.core.management import call_command

    call_command("migrate", verbosity=0, interactive=False)
    from django.db import connections

    connections.close_all()


def remove_work(path):
    """Delete a world's working data directory (git checkouts, caches); nothing private is lost:
    the ledger and the state dump are written to the output directory before this is called."""
    shutil.rmtree(path, ignore_errors=True)
