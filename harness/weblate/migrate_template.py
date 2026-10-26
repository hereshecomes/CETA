"""Create and migrate the Weblate template database once (run in its own process).

Usage: migrate_template.py [--force]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import weblate_env  # noqa: E402

server, host, port = weblate_env.start_cluster()
if weblate_env.db_exists(server, weblate_env.TEMPLATE_DB) and "--force" not in sys.argv:
    print(f"template {weblate_env.TEMPLATE_DB} exists (use --force to recreate)")
    sys.exit(0)
t = time.time()
weblate_env.migrate_template(server, host, port, weblate_env.WORK_ROOT / "_template_data")
print(f"migrated {weblate_env.TEMPLATE_DB} in {time.time() - t:.0f}s; host={host} port={port}")
