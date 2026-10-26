"""Run Indico 3.3.14 in-process against a per-world PostgreSQL database and a user-space Redis.

All application behaviour (authentication, permission checks, event/category logs, views) is Indico's
own code from the installed wheel (DATA/envs/indico). The harness only (a) provides the database, the
Redis instance and the data directories through Indico's documented configuration file (INDICO_CONFIG),
(b) controls the clock with time-machine, and (c) drives HTTP requests through Flask's test client
(WSGI) with the acting user's session.

PostgreSQL: the pgserver build of PostgreSQL 16.2 used for Weblate (it ships the contrib extensions
`unaccent` and `pg_trgm` that Indico's schema needs; Indico needs no ICU collation), run with
initdb/pg_ctl on a unix socket. One cluster hosts a prepared template database (TEMPLATE_DB) and one
database per world (CREATE DATABASE ... TEMPLATE ...).
Redis: redis-server 7.2.7 built from source, on a unix socket without persistence (Indico keeps
its cache and its sessions in Redis; the Celery broker URL points at it too, no worker runs).
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Data directory for the application environments, sources and executed worlds (see README):
# $CETA_DATA, default <repository>/data.
DATA = Path(os.environ.get("CETA_DATA", str(HERE.parents[1] / "data")))
LOCAL = Path(os.environ.get("CETA_TMP", "/tmp/ceta")) / "in"  # container-local disk: fast, regenerable (cluster, redis, working dirs)
PG_ROOT = LOCAL / "pg_cluster"
WORK_ROOT = LOCAL / "worlds"
WORLDS_ROOT = DATA / "worlds" / "indico"  # executed worlds: ledgers and state dumps
TEMPLATE_DB = "in_template"
PY = DATA / "envs" / "indico" / "bin" / "python"
PG_BIN = DATA / "envs" / "weblate" / "lib" / "python3.12" / "site-packages" / "pgserver" / "pginstall" / "bin"
PG_PORT = "54331"  # only used for the unix socket name; no TCP listener
REDIS_SERVER = DATA / "src" / "redis_src" / "redis-7.2.7" / "src" / "redis-server"
REDIS_SOCK = LOCAL / "redis.sock"
# Indico pins psycopg2 (source build); it was compiled against the libpq of DATA/envs/pg16icu (PostgreSQL
# 16.2 built from source with ICU). That directory is not on the loader path, so the library is preloaded
# here (before psycopg2 is imported) and LD_LIBRARY_PATH is passed to subprocesses.
LIBPQ_DIR = DATA / "envs" / "pg16icu" / "lib"
os.environ["LD_LIBRARY_PATH"] = str(LIBPQ_DIR) + ":" + os.environ.get("LD_LIBRARY_PATH", "")
try:
    import ctypes

    ctypes.CDLL(str(LIBPQ_DIR / "libpq.so.5"), mode=ctypes.RTLD_GLOBAL)
except OSError:
    pass


class PgCluster:
    def __init__(self, pgdata=PG_ROOT):
        self.pgdata = Path(pgdata)
        self.sock_dir = self.pgdata

    def _run(self, *args, check=True, **kw):
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = str(PG_BIN.parent / "lib") + ":" + env.get("LD_LIBRARY_PATH", "")
        return subprocess.run([str(PG_BIN / args[0]), *args[1:]], check=check, text=True, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)

    def start(self):
        self.pgdata.parent.mkdir(parents=True, exist_ok=True)
        if not (self.pgdata / "PG_VERSION").exists():
            self._run("initdb", "-D", str(self.pgdata), "-U", "postgres", "--auth=trust", "--encoding=UTF8",
                      "--locale=C.UTF-8")
        if self._run("pg_ctl", "-D", str(self.pgdata), "status", check=False).returncode != 0:
            self._run("pg_ctl", "-D", str(self.pgdata), "-l", str(self.pgdata.parent / "postgres.log"), "-w",
                      "-o", f"-k {self.sock_dir} -h '' -p {PG_PORT} -c max_connections=300 -c fsync=off "
                            f"-c synchronous_commit=off -c full_page_writes=off", "start")
        return self

    def stop(self):
        self._run("pg_ctl", "-D", str(self.pgdata), "-m", "fast", "stop", check=False)

    def psql(self, sql, db="postgres"):
        r = self._run("psql", "-h", str(self.sock_dir), "-p", PG_PORT, "-U", "postgres", "-d", db, "-v",
                      "ON_ERROR_STOP=1", "-X", "-At", "-c", sql, check=False)
        if r.returncode != 0:
            raise RuntimeError(f"psql failed: {r.stdout[-500:]}")
        return r.stdout


def start_cluster():
    return PgCluster().start()


def db_exists(server, name):
    return server.psql(f"SELECT 1 FROM pg_database WHERE datname = '{name}';").strip() == "1"


def create_db(server, name, template=None):
    if db_exists(server, name):
        server.psql(f"DROP DATABASE {name} WITH (FORCE);")
    server.psql(f"CREATE DATABASE {name}" + (f" TEMPLATE {template};" if template else ";"))


def drop_db(server, name):
    if db_exists(server, name):
        server.psql(f"DROP DATABASE {name} WITH (FORCE);")


def _redis_alive(sock):
    try:
        import redis

        return redis.Redis(unix_socket_path=str(sock), socket_connect_timeout=1).ping()
    except Exception:
        return False


def start_redis(sock=REDIS_SOCK):
    sock = Path(sock)
    sock.parent.mkdir(parents=True, exist_ok=True)
    if _redis_alive(sock):
        return str(sock)
    subprocess.run([str(REDIS_SERVER), "--port", "0", "--unixsocket", str(sock), "--unixsocketperm", "700",
                    "--save", "", "--appendonly", "no", "--daemonize", "yes", "--databases", "64",
                    "--pidfile", str(sock.parent / "redis.pid"), "--logfile", str(sock.parent / "redis.log"),
                    "--dir", str(sock.parent)], check=True)
    for _ in range(50):
        if _redis_alive(sock):
            return str(sock)
        time.sleep(0.1)
    raise RuntimeError(f"redis-server did not come up on {sock}")


CONFIG_TEMPLATE = """# Indico configuration written by harness/indico/indico_env.py (documented settings only)
SQLALCHEMY_DATABASE_URI = 'postgresql://postgres@/{db}?host={pg_host}&port={pg_port}'
SECRET_KEY = 'ceta-indico-testbed-secret-key-0123456789abcdef'
BASE_URL = 'http://indico.corp.example'
USE_PROXY = False
DEFAULT_TIMEZONE = 'UTC'
DEFAULT_LOCALE = 'en_GB'
REDIS_CACHE_URL = 'unix://{redis}?db={redis_db}'
CELERY_BROKER = 'redis+socket://{redis}?virtual_host={redis_db_broker}'
CACHE_DIR = '{data}/cache'
TEMP_DIR = '{data}/tmp'
LOG_DIR = '{data}/log'
STORAGE_BACKENDS = {{'default': 'fs:{data}/archive'}}
ATTACHMENT_STORAGE = 'default'
NO_REPLY_EMAIL = 'noreply@corp.example'
SUPPORT_EMAIL = 'support@corp.example'
PUBLIC_SUPPORT_EMAIL = 'support@corp.example'
SMTP_SERVER = ('localhost', 25)
SMTP_USE_TLS = False
SMTP_USE_CELERY = False
ENABLE_ROOMBOOKING = False
LOCAL_IDENTITIES = True
LOCAL_REGISTRATION = False
DEBUG = False
"""


def write_config(db, data_dir, redis_db=0):
    data_dir = Path(data_dir)
    for sub in ("cache", "tmp", "log", "archive"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    sock = start_redis()
    cfg = data_dir / "indico.conf"
    cfg.write_text(CONFIG_TEMPLATE.format(db=db, pg_host=str(PG_ROOT), pg_port=PG_PORT, redis=sock,
                                          redis_db=redis_db, redis_db_broker=(redis_db + 32) % 64,
                                          data=str(data_dir)))
    return cfg


def configure(db, data_dir, redis_db=0, fresh=True):
    """Write the configuration file for a world and point Indico at it (INDICO_CONFIG); returns the app."""
    data_dir = Path(data_dir)
    if fresh and data_dir.exists():
        shutil.rmtree(data_dir)
    cfg = write_config(db, data_dir, redis_db)
    import redis

    # one Redis database per concurrently running world (Indico caches per object id); start empty
    for n in (redis_db, (redis_db + 32) % 64):
        redis.Redis(unix_socket_path=str(REDIS_SOCK), db=n).flushdb()
    os.environ["INDICO_CONFIG"] = str(cfg)
    from indico.web.flask.app import make_app

    return make_app(testing=False)


def remove_work(path):
    shutil.rmtree(path, ignore_errors=True)
