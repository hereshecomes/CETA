"""Django settings for running Weblate in-process in the testbed.

Everything application-side (permission checks, audit writes, API views, periodic task code) is
Weblate's own. This module only points Weblate at a per-world PostgreSQL database (user-space
pgserver), a per-world data directory, in-memory cache/mail, and eager Celery (tasks run inline,
as they would in a worker). Values come from environment variables set by weblate_env.py.
"""
import os

from weblate.settings_example import *  # noqa: F401,F403

DEBUG = False
SECRET_KEY = "testbed-secret-key-not-for-production-0123456789"
SITE_TITLE = "Translations"
SITE_DOMAIN = "translate.example"
SITE_URL = f"http://{SITE_DOMAIN}"
ALLOWED_HOSTS = ["*"]
ADMINS = (("Testbed admin", "admin@example.invalid"),)
DEFAULT_FROM_EMAIL = "noreply@example.invalid"
SERVER_EMAIL = DEFAULT_FROM_EMAIL

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["WL_DB_NAME"],
        "USER": os.environ.get("WL_DB_USER", "postgres"),
        "PASSWORD": "",
        "HOST": os.environ["WL_DB_HOST"],  # unix socket directory of pgserver
        "PORT": os.environ.get("WL_DB_PORT", ""),
        "OPTIONS": {},
        "CONN_MAX_AGE": 0,
        "CONN_HEALTH_CHECKS": False,
        "DISABLE_SERVER_SIDE_CURSORS": False,
    }
}

DATA_DIR = os.environ["WL_DATA_DIR"]
CACHE_DIR = os.path.join(DATA_DIR, "cache")
MEDIA_ROOT = os.path.join(DATA_DIR, "media")
STATIC_ROOT = os.path.join(CACHE_DIR, "static")

CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "avatar": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

CELERY_TASK_ALWAYS_EAGER = True
CELERY_BROKER_URL = "memory://"
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_RESULT_BACKEND = None
CELERY_BEAT_SCHEDULER = "celery.beat:PersistentScheduler"

BACKGROUND_ADMIN_CHECKS = False
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STATS_LAZY = True
VCS_API_DELAY = 0
AUTO_UPDATE = False
REQUIRE_LOGIN = False
AUTHENTICATION_BACKENDS = (
    "social_core.backends.email.EmailAuth",
    "weblate.accounts.auth.WeblateUserBackend",
)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"level": "ERROR", "class": "logging.StreamHandler"}},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "weblate": {"handlers": ["console"], "level": "ERROR"},
        "social": {"handlers": ["console"], "level": "ERROR"},
        "celery": {"handlers": ["console"], "level": "ERROR"},
    },
}
