"""Pure helpers for deployable persistence and cache configuration.

Keeping URL parsing and validation outside ``settings.py`` makes the production
contract testable without reloading Django's global settings module. Local
development still has a zero-service fallback; a deployment that explicitly
asks for the production contract fails closed when one of its dependencies is
missing.
"""

from urllib.parse import urlparse

import dj_database_url
from django.core.exceptions import ImproperlyConfigured


FILESYSTEM_STORAGE = 'filesystem'
S3_STORAGE = 's3'
PRODUCTION_STORAGE_BACKENDS = {'s3', 'minio'}
PRODUCTION_SECRET_KEY_MIN_LENGTH = 32
PLACEHOLDER_MARKERS = ('change_me', 'change-me', 'replace_me', 'replace-me')


def _looks_like_placeholder(value):
    normalized = (value or '').strip().lower()
    return any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def database_config(
    database_url=None,
    *,
    sqlite_path,
    conn_max_age=0,
    conn_health_checks=True,
):
    """Return a Django DATABASES entry for a URL or local SQLite fallback."""
    if not database_url:
        return {
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': sqlite_path,
            },
        }

    config = dj_database_url.parse(
        database_url,
        conn_max_age=conn_max_age,
        conn_health_checks=conn_health_checks,
    )
    if not config.get('ENGINE'):
        raise ImproperlyConfigured('DJANGO_DATABASE_URL 无法识别数据库引擎。')
    return {'default': config}


def cache_config(
    cache_url=None,
    *,
    key_prefix='campus-glimmer',
    socket_connect_timeout=3,
    socket_timeout=3,
):
    """Return Redis-backed cache settings when configured, otherwise LocMem."""
    if not cache_url:
        return {
            'default': {
                'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                'LOCATION': key_prefix,
            },
        }

    parsed = urlparse(cache_url)
    scheme = parsed.scheme.lower()
    if scheme not in {'redis', 'rediss'}:
        raise ImproperlyConfigured(
            'DJANGO_CACHE_URL 必须使用 redis:// 或 rediss://。',
        )
    if not parsed.hostname:
        raise ImproperlyConfigured('DJANGO_CACHE_URL 必须包含 Redis 主机名。')
    return {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': cache_url,
            'KEY_PREFIX': key_prefix,
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'IGNORE_EXCEPTIONS': False,
                'SOCKET_CONNECT_TIMEOUT': socket_connect_timeout,
                'SOCKET_TIMEOUT': socket_timeout,
            },
        },
    }


def storage_config(
    backend=FILESYSTEM_STORAGE,
    *,
    s3_options=None,
    staticfiles_backend='django.contrib.staticfiles.storage.StaticFilesStorage',
):
    """Return Django STORAGES settings for local files or S3-compatible storage."""
    normalized = (backend or FILESYSTEM_STORAGE).lower()
    if normalized in {'filesystem', 'local'}:
        return {
            'default': {
                'BACKEND': 'django.core.files.storage.FileSystemStorage',
            },
            'staticfiles': {'BACKEND': staticfiles_backend},
        }
    if normalized in PRODUCTION_STORAGE_BACKENDS:
        options = dict(s3_options or {})
        if not options.get('bucket_name'):
            raise ImproperlyConfigured('启用对象存储时必须配置 DJANGO_S3_BUCKET。')
        return {
            'default': {
                'BACKEND': 'campus_glimmer.storage.PublicEndpointS3Storage',
                'OPTIONS': options,
            },
            'staticfiles': {'BACKEND': staticfiles_backend},
        }
    raise ImproperlyConfigured(
        'DJANGO_STORAGE_BACKEND 只支持 filesystem 或 s3。',
    )


def validate_production_services(
    *,
    debug,
    required,
    database_url,
    cache_url,
    storage_backend,
    secret_key='',
):
    """Fail closed when a deployment opts into the production dependency set."""
    if debug or not required:
        return

    problems = []
    if (
        not secret_key
        or secret_key.startswith('django-insecure')
        or len(secret_key) < PRODUCTION_SECRET_KEY_MIN_LENGTH
        or _looks_like_placeholder(secret_key)
    ):
        problems.append('DJANGO_SECRET_KEY 必须是非占位且至少 32 位的随机值')
    if not database_url:
        problems.append('DJANGO_DATABASE_URL')
    elif urlparse(database_url).scheme.lower() not in {'postgres', 'postgresql'}:
        problems.append('DJANGO_DATABASE_URL 必须指向 PostgreSQL')
    elif _looks_like_placeholder(database_url):
        problems.append('DJANGO_DATABASE_URL 仍包含占位值')
    if not cache_url:
        problems.append('DJANGO_CACHE_URL')
    elif urlparse(cache_url).scheme.lower() not in {'redis', 'rediss'}:
        problems.append('DJANGO_CACHE_URL 必须使用 redis:// 或 rediss://')
    elif _looks_like_placeholder(cache_url):
        problems.append('DJANGO_CACHE_URL 仍包含占位值')
    if (storage_backend or FILESYSTEM_STORAGE).lower() not in PRODUCTION_STORAGE_BACKENDS:
        problems.append('DJANGO_STORAGE_BACKEND 必须为 s3 或 minio')

    if problems:
        raise ImproperlyConfigured(
            '生产依赖配置不完整：' + '；'.join(problems) + '。',
        )
