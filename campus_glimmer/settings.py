import os
from pathlib import Path
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

from .deployment import (
    cache_config,
    database_config,
    storage_config,
    validate_production_services,
)

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'django-insecure-local-development-only-change-me')
DEBUG = os.getenv('DJANGO_DEBUG', '1').lower() in {'1', 'true', 'yes'}
ALLOWED_HOSTS = [host.strip() for host in os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost,testserver').split(',') if host.strip()]
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.getenv('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if origin.strip()]


def _env_bool(name, default=False):
    fallback = '1' if default else '0'
    return os.getenv(name, fallback).lower() in {'1', 'true', 'yes', 'on'}


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_tristate(name):
    # 三态开关：没设置时返回 None，由调用方按环境自己判断；设置了就只认显式的真假。
    # 这里故意不像 _env_bool 那样静默默认：DJANGO_METRICS_SHARED=flase 这种拼错的
    # 开关一旦被当成「关」，排查的人会以为共享从来就没开过。
    raw = (os.getenv(name) or '').strip().lower()
    if not raw:
        return None
    if raw in {'1', 'true', 'yes', 'on'}:
        return True
    if raw in {'0', 'false', 'no', 'off'}:
        return False
    raise ImproperlyConfigured(
        '%s 只能是 1/0 这类布尔值，现在是 %r' % (name, raw)
    )


def _share_pacing(interval, ttl):
    # 发布节奏的校验必须在启动时就炸：TTL 小于等于间隔时，一个活着的实例也会
    # 在两次发布之间从舰队视图里过期，症状是看板上的实例数来回跳，而没有任何
    # 东西指向这个配置项。间隔为 0 同理，那会把发布线程变成忙循环。
    if ttl <= interval or interval <= 0:
        raise ImproperlyConfigured(
            'DJANGO_METRICS_SHARE_TTL 必须大于 DJANGO_METRICS_SHARE_INTERVAL，'
            '且间隔必须为正数（当前 TTL=%r，间隔=%r）' % (ttl, interval)
        )
    return interval, ttl


def _hsts_seconds(value=None):
    """Read HSTS max-age defensively: a bad deploy value must not stop boot."""
    raw = os.getenv('DJANGO_SECURE_HSTS_SECONDS', '0') if value is None else value
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 0


INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'accounts',
    'listings',
    'chat_messages',
]

MIDDLEWARE = [
    # 放在最前面：关联 ID 必须在任何可能写日志的中间件之前就定下来，
    # 否则排在它后面的组件报警时，日志里还是占位符。
    'campus_glimmer.middleware.RequestIdMiddleware',
    # 紧跟关联 ID 之后：它要读 resolver_match，而 URL 解析发生在整个中间件链
    # 的最内层，位置前后不影响取值；但它必须排在会短路请求的中间件之前，
    # 否则被短路的请求一条指标都不留。
    'campus_glimmer.metrics.MetricsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    # 动态响应此前完全不走压缩：看板一页 580KB 左右的 HTML 原样下发，普通页面
    # 也有 16KB 左右。部署是 gunicorn 直出、没有反向代理，所以只能在这里压。
    # 放在 WhiteNoise 之前，保证拿到的是最终响应体；Django 5.2 会把强 ETag
    # 自动降级为弱 ETag，不会破坏条件请求。
    'django.middleware.gzip.GZipMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'campus_glimmer.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'campus_glimmer.context_processors.unread_counts',
                'campus_glimmer.context_processors.navigation_state',
            ],
        },
    },
]

WSGI_APPLICATION = 'campus_glimmer.wsgi.application'

DATABASE_URL = os.getenv('DJANGO_DATABASE_URL', '').strip()
DATABASES = database_config(
    DATABASE_URL,
    sqlite_path=os.getenv('DJANGO_DB_PATH', str(BASE_DIR / 'db.sqlite3')),
    conn_max_age=_env_int('DJANGO_DATABASE_CONN_MAX_AGE', 0 if DEBUG else 60),
    conn_health_checks=True,
)

CACHE_URL = os.getenv('DJANGO_CACHE_URL', '').strip()
CACHES = cache_config(
    CACHE_URL,
    key_prefix=os.getenv('DJANGO_CACHE_KEY_PREFIX', 'campus-glimmer'),
    socket_connect_timeout=_env_int('DJANGO_CACHE_CONNECT_TIMEOUT', 3),
    socket_timeout=_env_int('DJANGO_CACHE_SOCKET_TIMEOUT', 3),
)
SESSION_ENGINE = os.getenv('DJANGO_SESSION_ENGINE', 'django.contrib.sessions.backends.db')

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
WHITENOISE_MAX_AGE = 31_536_000
MEDIA_URL = os.getenv('DJANGO_MEDIA_URL', '/media/')
MEDIA_ROOT = BASE_DIR / 'media'

STORAGE_BACKEND = os.getenv('DJANGO_STORAGE_BACKEND', 'filesystem').strip()
S3_OPTIONS = {
    'bucket_name': os.getenv('DJANGO_S3_BUCKET', '').strip(),
    'region_name': os.getenv('DJANGO_S3_REGION', 'us-east-1').strip(),
    'endpoint_url': os.getenv('DJANGO_S3_ENDPOINT_URL', '').strip() or None,
    'access_key': os.getenv('DJANGO_S3_ACCESS_KEY_ID', '').strip() or None,
    'secret_key': os.getenv('DJANGO_S3_SECRET_ACCESS_KEY', '').strip() or None,
    'custom_domain': os.getenv('DJANGO_S3_CUSTOM_DOMAIN', '').strip() or None,
    'public_endpoint_url': os.getenv('DJANGO_S3_PUBLIC_ENDPOINT_URL', '').strip() or None,
    'addressing_style': os.getenv('DJANGO_S3_ADDRESSING_STYLE', 'path').strip(),
    'signature_version': os.getenv('DJANGO_S3_SIGNATURE_VERSION', 's3v4').strip(),
    'querystring_auth': _env_bool('DJANGO_S3_QUERYSTRING_AUTH', True),
    'querystring_expire': _env_int('DJANGO_S3_QUERYSTRING_EXPIRE', 3600),
    'file_overwrite': False,
    'default_acl': None,
    'location': os.getenv('DJANGO_S3_LOCATION', '').strip() or '',
}
STATICFILES_BACKEND = (
    'whitenoise.storage.CompressedManifestStaticFilesStorage'
    if not DEBUG
    else 'django.contrib.staticfiles.storage.StaticFilesStorage'
)
STORAGES = storage_config(
    STORAGE_BACKEND,
    s3_options=S3_OPTIONS,
    staticfiles_backend=STATICFILES_BACKEND,
)

REQUIRE_PRODUCTION_SERVICES = _env_bool('DJANGO_REQUIRE_PRODUCTION_SERVICES', False)
validate_production_services(
    debug=DEBUG,
    required=REQUIRE_PRODUCTION_SERVICES,
    database_url=DATABASE_URL,
    cache_url=CACHE_URL,
    storage_backend=STORAGE_BACKEND,
    secret_key=SECRET_KEY,
)

# Django 自带的默认日志把 console handler 挂在 require_debug_true 上，也就是
# 生产环境什么都不往 stdout 写，日志收集器只能收到空气。这里显式接管：
# stdout 始终可用、每行都带请求关联 ID，级别由 DJANGO_LOG_LEVEL 控制。
LOG_LEVEL = os.getenv('DJANGO_LOG_LEVEL', 'INFO').strip().upper() or 'INFO'

# /metrics/ 的访问令牌。留空即整个端点返回 404——监控端点暴露的是内部拓扑
# （有哪些路由、大致多快），不该默认对外可见。令牌只在这里读，不进任何模板。
METRICS_TOKEN = os.getenv('DJANGO_METRICS_TOKEN', '').strip()
METRICS_SERVICE_NAME = os.getenv('DJANGO_METRICS_SERVICE_NAME', 'campus-glimmer').strip() \
    or 'campus-glimmer'

# 告警状态翻转时的可选 Webhook。地址留空即关闭；它和指标令牌是两件事：前者
# 决定「谁被喊醒」，后者决定「谁能读取观测端点」。URL 在启动时校验，避免部署
# 后才在告警真正发生的那一刻发现地址写错。
ALERT_WEBHOOK_URL = os.getenv('DJANGO_ALERT_WEBHOOK_URL', '').strip()
ALERT_WEBHOOK_BEARER_TOKEN = os.getenv('DJANGO_ALERT_WEBHOOK_BEARER_TOKEN', '').strip()
ALERT_WEBHOOK_TIMEOUT = _env_float('DJANGO_ALERT_WEBHOOK_TIMEOUT', 2.0)
if ALERT_WEBHOOK_TIMEOUT <= 0:
    raise ImproperlyConfigured('DJANGO_ALERT_WEBHOOK_TIMEOUT 必须为正数')
if ALERT_WEBHOOK_URL:
    _alert_webhook = urlparse(ALERT_WEBHOOK_URL)
    if _alert_webhook.scheme not in {'http', 'https'} or not _alert_webhook.netloc:
        raise ImproperlyConfigured(
            'DJANGO_ALERT_WEBHOOK_URL 必须是完整的 http:// 或 https:// 地址'
        )

# 跨副本指标聚合。留空表示按缓存后端自己判断：LocMem 是进程内的，多 worker 下各存
# 各的，开了只会给出「只有我自己」的假象；Redis 一类跨进程后端默认开。启用后要把
# Prometheus 的抓取目标只留一个 /metrics/——每个副本返回的都是同一份合并视图，
# 重复抓取会把总量按副本数翻倍。
METRICS_SHARED = _env_tristate('DJANGO_METRICS_SHARED')

# 快照发布间隔与存活时间。校验放在 _share_pacing 里，启动时就炸。
METRICS_SHARE_INTERVAL, METRICS_SHARE_TTL = _share_pacing(
    _env_float('DJANGO_METRICS_SHARE_INTERVAL', 5.0),
    _env_float('DJANGO_METRICS_SHARE_TTL', 30.0),
)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse',
        },
        'request_id': {
            '()': 'campus_glimmer.middleware.RequestIdFilter',
        },
    },
    'formatters': {
        'structured': {
            'format': (
                '%(asctime)s %(levelname)s [%(request_id)s] '
                '%(name)s %(message)s'
            ),
            'datefmt': '%Y-%m-%d %H:%M:%S',
        },
        'django.server': {
            'format': '[%(asctime)s] %(message)s',
            'datefmt': '%d/%b/%Y %H:%M:%S',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': LOG_LEVEL,
            'formatter': 'structured',
            'filters': ['request_id'],
        },
        'django.server': {
            'class': 'logging.StreamHandler',
            'level': 'INFO',
            'formatter': 'django.server',
            'filters': ['request_id'],
        },
        # Django 默认行为：DEBUG=False 时才给管理员发邮件。
        'mail_admins': {
            'class': 'django.utils.log.AdminEmailHandler',
            'level': 'ERROR',
            'filters': ['require_debug_false'],
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'mail_admins'],
            'level': LOG_LEVEL,
        },
        'django.server': {
            'handlers': ['django.server'],
            'level': 'INFO',
            'propagate': False,
        },
        # 健康探针每 30 秒打一次 INFO，日志量比业务请求还大，单独压到 WARNING。
        'campus_glimmer.health': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

LOGIN_REDIRECT_URL = 'home'
LOGIN_URL = 'login'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
EMAIL_BACKEND = os.getenv(
    'DJANGO_EMAIL_BACKEND',
    'django.core.mail.backends.console.EmailBackend' if DEBUG else 'django.core.mail.backends.smtp.EmailBackend',
)
DEFAULT_FROM_EMAIL = os.getenv('DJANGO_DEFAULT_FROM_EMAIL', 'no-reply@campus-glimmer.local')

# 浏览器安全基线不跟随 DEBUG：本地、测试、预发与生产保持同一组默认头。
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_CROSS_ORIGIN_OPENER_POLICY = 'same-origin'
X_FRAME_OPTIONS = 'DENY'
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True

# 反向代理 / HTTPS 部署时可通过环境变量开启这些安全选项。
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = os.getenv('DJANGO_SECURE_SSL_REDIRECT', '0').lower() in {'1', 'true', 'yes'}
SESSION_COOKIE_SECURE = os.getenv('DJANGO_SESSION_COOKIE_SECURE', '0').lower() in {'1', 'true', 'yes'}
CSRF_COOKIE_SECURE = os.getenv('DJANGO_CSRF_COOKIE_SECURE', '0').lower() in {'1', 'true', 'yes'}
SECURE_HSTS_SECONDS = _hsts_seconds()
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0
