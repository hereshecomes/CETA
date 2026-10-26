"""Create the template database for Indico 3.3.14 worlds (run with the Indico venv's python).

Steps: start the user-space cluster and Redis, create database TEMPLATE_DB with the extensions Indico
requires (unaccent, pg_trgm), then run Indico's own `indico db prepare` (creates every table, stamps
alembic to head and adds Indico's initial data such as the root category). Usage:
    prepare_template.py [--force]
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indico_env  # noqa: E402


def main():
    force = "--force" in sys.argv
    server = indico_env.start_cluster()
    if indico_env.db_exists(server, indico_env.TEMPLATE_DB) and not force:
        print("template exists")
        return
    indico_env.create_db(server, indico_env.TEMPLATE_DB)
    server.psql("CREATE EXTENSION IF NOT EXISTS unaccent; CREATE EXTENSION IF NOT EXISTS pg_trgm;",
                db=indico_env.TEMPLATE_DB)
    data = indico_env.LOCAL / "_template_data"
    cfg = indico_env.write_config(indico_env.TEMPLATE_DB, data)
    env = dict(os.environ, INDICO_CONFIG=str(cfg))
    t0 = time.time()
    r = subprocess.run([str(indico_env.PY), "-c", "from indico.cli.core import cli; cli()", "db", "prepare"], env=env, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(r.stdout[-3000:])
    print(f"indico db prepare rc={r.returncode} in {time.time() - t0:.1f}s")
    if r.returncode != 0:
        sys.exit(1)
    server.psql(f"ALTER DATABASE {indico_env.TEMPLATE_DB} WITH IS_TEMPLATE true;")


if __name__ == "__main__":
    main()
