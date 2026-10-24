"""Real S3/MinIO integration coverage for the object storage layer.

These cases exercise the same storage class production uses, against a real
S3-compatible endpoint. CI provides one through the ``object-storage`` job;
locally the whole module stays dormant unless ``DJANGO_S3_ENDPOINT_URL``
points at a reachable bucket, so the zero-service test run is unchanged.

The distinction that matters: *not configured* means skip, *configured but
unreachable* means fail. A silent skip in CI would let "object storage is
verified" stay a claim in the docs while nothing was actually uploaded.
"""

import importlib.util
import json
import os
import socket
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import SimpleTestCase, override_settings

from . import health
from .deployment import storage_config
from .storage import PublicEndpointS3Storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b'campus-glimmer object storage smoke bytes\n'
TRUTHY = {'1', 'true', 'yes', 'on'}


def load_backup_module():
    path = PROJECT_ROOT / 'docker' / 'backup.py'
    spec = importlib.util.spec_from_file_location('campus_backup', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _env(name, default=''):
    return os.getenv(name, default).strip()


def _env_bool(name, default):
    return _env(name, '1' if default else '0').lower() in TRUTHY


def _env_int(name, default):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _s3_options(**overrides):
    """Mirror ``settings.S3_OPTIONS`` so the test never invents its own shape."""
    options = {
        'bucket_name': _env('DJANGO_S3_BUCKET', 'campus-glimmer'),
        'region_name': _env('DJANGO_S3_REGION', 'us-east-1'),
        'endpoint_url': _env('DJANGO_S3_ENDPOINT_URL') or None,
        'access_key': _env('DJANGO_S3_ACCESS_KEY_ID', 'minioadmin') or None,
        'secret_key': _env('DJANGO_S3_SECRET_ACCESS_KEY', 'minioadmin') or None,
        'custom_domain': _env('DJANGO_S3_CUSTOM_DOMAIN') or None,
        'public_endpoint_url': _env('DJANGO_S3_PUBLIC_ENDPOINT_URL') or None,
        'addressing_style': _env('DJANGO_S3_ADDRESSING_STYLE', 'path'),
        'signature_version': _env('DJANGO_S3_SIGNATURE_VERSION', 's3v4'),
        'querystring_auth': _env_bool('DJANGO_S3_QUERYSTRING_AUTH', True),
        'querystring_expire': _env_int('DJANGO_S3_QUERYSTRING_EXPIRE', 3600),
        'file_overwrite': False,
        'default_acl': None,
        'location': _env('DJANGO_S3_LOCATION', ''),
    }
    options.update(overrides)
    return options


def _raw_client(options=None):
    options = options or _s3_options()
    return boto3.session.Session().client(
        's3',
        endpoint_url=options['endpoint_url'],
        region_name=options['region_name'],
        aws_access_key_id=options['access_key'],
        aws_secret_access_key=options['secret_key'],
        config=Config(
            signature_version=options['signature_version'],
            s3={'addressing_style': options['addressing_style']},
        ),
    )


def _endpoint_reachable():
    """TCP-level liveness, kept separate from the S3 call it guards.

    "Nothing is listening" is a local, expected state and must stay a skip.
    "Something answered but the bucket is unusable" is a broken deployment and
    must fail, otherwise a CI job that forgets to create the bucket would pass
    with three quiet skips and the docs would keep claiming verified storage.
    """
    endpoint = urlparse(_env('DJANGO_S3_ENDPOINT_URL'))
    host = endpoint.hostname
    if not host:
        return False
    port = endpoint.port or (443 if endpoint.scheme == 'https' else 80)
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def _probe():
    """Return ``(client, reason, reachable)`` for the configured bucket."""
    if not _env('DJANGO_S3_ENDPOINT_URL'):
        return None, '未配置 DJANGO_S3_ENDPOINT_URL', False
    options = _s3_options()
    if not options['bucket_name']:
        return None, '未配置 DJANGO_S3_BUCKET', _endpoint_reachable()
    reachable = _endpoint_reachable()
    if not reachable:
        return None, '端点 %s 不可达' % options['endpoint_url'], False
    try:
        client = _raw_client(options)
        client.head_bucket(Bucket=options['bucket_name'])
    except Exception as exc:  # noqa: BLE001 - any failure means "not usable"
        return None, '%s: %s' % (type(exc).__name__, exc), True
    return client, None, True


PROBE_CLIENT, PROBE_REASON, PROBE_REACHED = _probe()
# 每个进程一个前缀：并行 worker 与连续两次运行互不干扰，tearDown 只清自己的。
RUN_PREFIX = 'ci-smoke/{}-{}'.format(
    datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'),
    uuid.uuid4().hex[:8],
)


def _fetch(url):
    """GET a URL, returning ``(status, body)`` even when the server refuses."""
    try:
        with urlopen(Request(url, method='GET'), timeout=15) as response:
            return response.status, response.read()
    except HTTPError as exc:
        with exc:
            return exc.code, exc.read()


class ObjectStorageTestCase(SimpleTestCase):
    """Shared skeleton for every case that needs a live bucket.

    The bucket client lives on ``cls.s3`` rather than ``cls.client``:
    ``SimpleTestCase.setUpClass`` installs Django's own test client there, so
    reusing that name silently replaces ours.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if PROBE_CLIENT is None:
            if PROBE_REACHED:
                raise AssertionError(
                    'DJANGO_S3_ENDPOINT_URL 已配置且端点可达，但对象存储不可用：%s'
                    % PROBE_REASON)
            raise unittest.SkipTest('对象存储端点不可达：%s' % PROBE_REASON)
        cls.s3 = PROBE_CLIENT
        cls.bucket = _s3_options()['bucket_name']
        cls.location = _s3_options()['location']
        cls.key_prefix = '{}{}'.format(
            cls.location + '/' if cls.location else '', RUN_PREFIX)
        cls.storage = PublicEndpointS3Storage(**_s3_options())
        # 走 settings 的真实入口，证明部署时 default_storage 拿到的就是它。
        cls._storage_override = override_settings(
            STORAGES=storage_config('s3', s3_options=_s3_options()))
        cls._storage_override.enable()
        cls.addClassCleanup(cls._storage_override.disable)
        cls.addClassCleanup(cls._cleanup_keys)

    @classmethod
    def _cleanup_keys(cls):
        """Drop every key this run created, including keys left by a failure."""
        paginator = cls.s3.get_paginator('list_objects_v2')
        keys = []
        for page in paginator.paginate(Bucket=cls.bucket, Prefix=cls.key_prefix):
            keys.extend(obj['Key'] for obj in page.get('Contents', []))
        for start in range(0, len(keys), 1000):
            cls.s3.delete_objects(
                Bucket=cls.bucket,
                Delete={'Objects': [{'Key': key} for key in keys[start:start + 1000]]},
            )

    def assertStatusIs(self, status, allowed, url):
        self.assertIn(
            status, allowed,
            'GET %s 返回 %s，期望 %s' % (url, status, allowed))


class ObjectStorageSettingsTests(SimpleTestCase):
    """配置层：证明环境变量确实能拼出生产用的存储后端。"""

    def test_s3_options_map_onto_the_public_endpoint_backend(self):
        stores = storage_config('s3', s3_options=_s3_options())
        self.assertEqual(
            stores['default']['BACKEND'],
            'campus_glimmer.storage.PublicEndpointS3Storage',
        )
        options = stores['default']['OPTIONS']
        self.assertEqual(options['bucket_name'], _s3_options()['bucket_name'])
        self.assertEqual(options['addressing_style'], 'path')
        self.assertEqual(options['signature_version'], 's3v4')
        self.assertFalse(options['file_overwrite'])

    def test_location_is_prefixed_onto_every_key(self):
        storage = PublicEndpointS3Storage(**_s3_options(location='media'))
        self.assertEqual(storage._normalize_name('a/b.txt'), 'media/a/b.txt')


class ObjectStorageRoundTripTests(ObjectStorageTestCase):
    """上传与回源：对象是否真的落在桶里，并且读得回来。"""

    def test_saved_object_round_trips_through_default_storage(self):
        name = default_storage.save(
            '%s/notes.txt' % RUN_PREFIX, ContentFile(PAYLOAD))

        self.assertTrue(name.startswith(RUN_PREFIX))
        self.assertTrue(default_storage.exists(name))
        self.assertEqual(default_storage.size(name), len(PAYLOAD))
        with default_storage.open(name) as handle:
            self.assertEqual(handle.read(), PAYLOAD)
        self.assertIsInstance(default_storage, PublicEndpointS3Storage)

    def test_saved_object_is_visible_to_a_raw_client(self):
        name = self.storage.save('%s/raw.txt' % RUN_PREFIX, ContentFile(PAYLOAD))
        key = self.storage._normalize_name(name)

        head = self.s3.head_object(Bucket=self.bucket, Key=key)
        self.assertEqual(head['ContentLength'], len(PAYLOAD))

        body = self.s3.get_object(Bucket=self.bucket, Key=key)['Body']
        with body:
            self.assertEqual(body.read(), PAYLOAD)

    def test_delete_removes_the_object_from_the_bucket(self):
        name = self.storage.save('%s/gone.txt' % RUN_PREFIX, ContentFile(PAYLOAD))
        self.storage.delete(name)
        self.assertFalse(self.storage.exists(name))
        with self.assertRaises(Exception):
            self.s3.head_object(
                Bucket=self.bucket, Key=self.storage._normalize_name(name))


class ObjectStorageAccessTests(ObjectStorageTestCase):
    """权限与签名 URL：私有对象不能裸取，签名链接必须真能取、且会过期。"""

    def test_signed_url_points_at_the_browser_endpoint_and_downloads(self):
        name = self.storage.save('%s/signed.txt' % RUN_PREFIX, ContentFile(PAYLOAD))
        url = self.storage.url(name)

        expected = (
            _env('DJANGO_S3_PUBLIC_ENDPOINT_URL')
            or _env('DJANGO_S3_ENDPOINT_URL')
        ).rstrip('/')
        self.assertTrue(
            url.startswith(expected),
            '签名 URL 的主机不是浏览器可达端点：%s' % url)
        self.assertIn('X-Amz-Signature=', url)
        self.assertIn('X-Amz-Expires=%d' % _env_int(
            'DJANGO_S3_QUERYSTRING_EXPIRE', 3600), url)

        status, body = _fetch(url)
        self.assertStatusIs(status, (200,), url)
        self.assertEqual(body, PAYLOAD)

    def test_expire_argument_reaches_the_presigned_url(self):
        name = self.storage.save('%s/expire.txt' % RUN_PREFIX, ContentFile(PAYLOAD))
        url = self.storage.url(name, expire=120)
        self.assertIn('X-Amz-Expires=120', url)

    def test_unsigned_object_url_is_refused_while_the_signed_one_works(self):
        name = self.storage.save('%s/private.txt' % RUN_PREFIX, ContentFile(PAYLOAD))
        unsigned = PublicEndpointS3Storage(
            **_s3_options(querystring_auth=False)).url(name)

        self.assertNotIn('X-Amz-Signature=', unsigned)
        status, _ = _fetch(unsigned)
        self.assertStatusIs(
            status, (401, 403),
            '桶策略放开了匿名下载，私有对象断言不成立：%s' % unsigned)

        status, body = _fetch(self.storage.url(name))
        self.assertStatusIs(status, (200,), unsigned)
        self.assertEqual(body, PAYLOAD)

    def test_tampered_signature_is_rejected(self):
        name = self.storage.save('%s/tampered.txt' % RUN_PREFIX, ContentFile(PAYLOAD))
        head, separator, query = self.storage.url(name).partition('?')
        signature = parse_qs(query)['X-Amz-Signature'][0]
        tampered = head + separator + query.replace(
            signature, '0' * len(signature), 1)
        self.assertNotEqual(tampered, head + separator + query)

        status, _ = _fetch(tampered)
        self.assertStatusIs(
            status, (400, 403),
            '签名被改写后仍然放行，说明签名没有被真正校验：%s' % tampered)

    def test_expired_signature_is_rejected(self):
        """过期签名必须被拒。

        这是 S3 的既有语义：``X-Amz-Expires`` 到点后同一链接不能再取。纯 Python
        的 S3 mock（moto）不实现这一层，只有 MinIO 这类真实服务会拒绝，所以这条
        用例的结论要以 CI 里的 MinIO 为准。
        """
        name = self.storage.save('%s/stale.txt' % RUN_PREFIX, ContentFile(PAYLOAD))
        url = self.storage.url(name, expire=1)
        time.sleep(2)

        status, body = _fetch(url)
        self.assertStatusIs(
            status, (400, 403),
            '过期签名仍被接受，说明 X-Amz-Expires 没有生效：%s -> %r'
            % (url, body[:200]))

    def test_readiness_probe_sees_the_bucket(self):
        health._probe_object_storage()


class ObjectStorageBackupRestoreTests(ObjectStorageTestCase):
    """备份恢复：对象镜像要能按原 key 写回桶里。"""

    def test_restore_objects_puts_bytes_back_at_original_keys(self):
        backup = load_backup_module()
        name = '%s/restored.txt' % RUN_PREFIX
        key = self.storage._normalize_name(name)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'objects').mkdir()
            (root / 'objects' / '00000000-restored.txt').write_bytes(PAYLOAD)
            dump = b'placeholder database dump'
            (root / 'database.dump').write_bytes(dump)
            manifest = {
                'format_version': backup.MANIFEST_FORMAT,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'database': {
                    'file': 'database.dump',
                    'size': len(dump),
                    'sha256': backup.sha256_file(root / 'database.dump'),
                },
                'object_storage': {
                    'bucket': self.bucket,
                    'count': 1,
                    'objects': [{
                        'key': key,
                        'file': 'objects/00000000-restored.txt',
                        'size': len(PAYLOAD),
                        'sha256': backup.sha256_file(
                            root / 'objects' / '00000000-restored.txt'),
                        'etag': '',
                        'last_modified': None,
                    }],
                },
            }
            (root / 'manifest.json').write_text(
                json.dumps(manifest), encoding='utf-8')

            # 先删掉，确保断言的是“恢复写回了对象”，而不是读到了残留。
            self.s3.delete_object(Bucket=self.bucket, Key=key)
            self.assertFalse(self.storage.exists(name))

            restored = backup.restore_objects(
                root, bucket=self.bucket, client=self.s3)

        self.assertEqual(restored, 1)
        self.assertTrue(self.storage.exists(name))
        with self.storage.open(name) as handle:
            self.assertEqual(handle.read(), PAYLOAD)
