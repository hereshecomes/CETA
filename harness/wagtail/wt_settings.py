"""Django settings for running Wagtail 8.0.1 in-process in the testbed.

Everything application-side (permission checks, workflow state machine, audit writes, admin views)
is Wagtail's own. This module only points Wagtail at a per-world SQLite database and data
directory, uses in-memory mail and the database search backend, and keeps every workflow-related
setting at its documented default (WAGTAIL_WORKFLOW_ENABLED, WAGTAIL_WORKFLOW_CANCEL_ON_PUBLISH,
WAGTAIL_FINISH_WORKFLOW_ACTION, WAGTAIL_WORKFLOW_REQUIRE_REAPPROVAL_ON_EDIT are not set).
Values come from environment variables set by wagtail_env.py. INSTALLED_APPS / MIDDLEWARE /
TEMPLATES follow Wagtail's own project template (wagtail/project_template).
"""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

SECRET_KEY = "testbed-secret-key-not-for-production-0123456789"
DEBUG = False
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "testsite",
    "wagtail.contrib.forms",
    "wagtail.contrib.redirects",
    "wagtail.embeds",
    "wagtail.sites",
    "wagtail.users",
    "wagtail.snippets",
    "wagtail.documents",
    "wagtail.images",
    "wagtail.search",
    "wagtail.admin",
    "wagtail",
    "modelcluster",
    "taggit",
    "django_filters",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "wagtail.contrib.redirects.middleware.RedirectMiddleware",
]

ROOT_URLCONF = "wt_urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ["WT_DB_PATH"],
    }
}

DATA_DIR = os.environ["WT_DATA_DIR"]
MEDIA_ROOT = os.path.join(DATA_DIR, "media")
STATIC_ROOT = os.path.join(DATA_DIR, "static")
STATIC_URL = "/static/"
MEDIA_URL = "/media/"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_NUMBER_FIELDS = 10_000

WAGTAIL_SITE_NAME = "Corporate Site"
WAGTAILADMIN_BASE_URL = "http://cms.example"
WAGTAILSEARCH_BACKENDS = {"default": {"BACKEND": "wagtail.search.backends.database"}}
WAGTAILDOCS_EXTENSIONS = ["csv", "docx", "key", "odt", "pdf", "pptx", "rtf", "txt", "xlsx", "zip"]

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"level": "ERROR", "class": "logging.StreamHandler"}},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "wagtail": {"handlers": ["console"], "level": "ERROR"},
    },
}
