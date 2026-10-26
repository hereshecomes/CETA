"""Run pretix in-process against a per-world SQLite copy of the migrated template.

All application behaviour (permission checks, API views, audit logging) is pretix's own
code. The harness only (a) chooses the database file, (b) controls the clock with
time-machine, and (c) drives HTTP requests through Django's WSGI handler.
"""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DB = ROOT / "app_env" / "template" / "template.sqlite3"


def configure(world_dir: Path) -> Path:
    world_dir.mkdir(parents=True, exist_ok=True)
    db = world_dir / "app.sqlite3"
    shutil.copyfile(TEMPLATE_DB, db)
    data = world_dir / "data"
    data.mkdir(exist_ok=True)
    cfg = world_dir / "pretix.cfg"
    cfg.write_text(
        "[pretix]\ninstance_name=ceta-world\nurl=http://127.0.0.1:8345\n"
        f"datadir={data}\ncurrency=EUR\n"
        f"[database]\nbackend=sqlite3\nname={db}\n"
        "[django]\ndebug=false\n"
        "[mail]\nfrom=noreply@example.invalid\nhost=127.0.0.1\nport=1\n"
    )
    os.environ["PRETIX_CONFIG_FILE"] = str(cfg)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pretix.settings")
    import django

    django.setup()
    return db
