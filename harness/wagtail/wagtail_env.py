"""Run Wagtail 8.0.1 in-process against a per-world SQLite database.

All application behaviour (permission checks, workflow state machine, admin views, audit log
writes) is Wagtail's own. The harness only (a) provides the database file and data directory,
(b) controls the clock with time-machine, and (c) drives HTTP requests through Django's test
client (WSGI handler) with the acting user's session.

Database layout: one migrated template database (TEMPLATE_DB, on container-local disk, rebuilt
by build_template() when missing) is copied to one file per world.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Data directory for the application environments, sources and executed worlds (see README):
# $CETA_DATA, default <repository>/data.
DATA = Path(os.environ.get("CETA_DATA", str(HERE.parents[1] / "data")))
LOCAL = Path(os.environ.get("CETA_TMP", "/tmp/ceta")) / "wt"  # container-local disk: fast, regenerable (template + working dirs)
TEMPLATE_DB = LOCAL / "template.sqlite3"
WORK_ROOT = LOCAL / "worlds"
WORLDS_ROOT = DATA / "worlds" / "wagtail"  # executed worlds: ledgers and state dumps
PY = DATA / "envs" / "wagtail" / "bin" / "python"


def configure(db_path, data_dir):
    """Point Wagtail at a database file and data directory, then django.setup() (once per process)."""
    data_dir = Path(data_dir)
    if data_dir.exists():
        shutil.rmtree(data_dir)
    for sub in ("media", "static"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    os.environ["WT_DB_PATH"] = str(db_path)
    os.environ["WT_DATA_DIR"] = str(data_dir)
    os.environ["DJANGO_SETTINGS_MODULE"] = "wt_settings"
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import django

    django.setup()


def build_template():
    """Create and migrate the template database in THIS process (call from a dedicated process)."""
    LOCAL.mkdir(parents=True, exist_ok=True)
    if TEMPLATE_DB.exists():
        TEMPLATE_DB.unlink()
    configure(TEMPLATE_DB, LOCAL / "_template_data")
    from django.core.management import call_command

    call_command("migrate", verbosity=0, interactive=False)
    from django.db import connections

    connections.close_all()


def ensure_template(force=False):
    """Build the template database in a subprocess if it is missing (or always with force)."""
    if TEMPLATE_DB.exists() and not force:
        return
    subprocess.run([str(PY), "-c", "import sys; sys.path.insert(0, %r); import wagtail_env; wagtail_env.build_template()" % str(HERE)],
                   check=True)


def clone_template(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    shutil.copy(TEMPLATE_DB, db_path)
    return db_path


def remove_work(path):
    """Delete a world's working directory (database file, media); the ledger and state dump were
    already written to the output directory."""
    shutil.rmtree(path, ignore_errors=True)
