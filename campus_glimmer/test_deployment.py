import hashlib
import importlib.util
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from .deployment import (
    cache_config,
    database_config,
    storage_config,
    validate_production_services,
)
from .storage import PublicEndpointS3Storage


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_backup_module():
    path = PROJECT_ROOT / 'docker' / 'backup.py'
    spec = importlib.util.spec_from_file_location('campus_backup', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeploymentConfigTests(SimpleTestCase):
    def test_database_defaults_to_sqlite_and_accepts_postgresql_url(self):
        local = database_config('', sqlite_path='/tmp/campus.sqlite3')
        self.assertEqual(local['default']['ENGINE'], 'django.db.backends.sqlite3')
        self.assertEqual(local['default']['NAME'], '/tmp/campus.sqlite3')

        remote = database_config(
            'postgresql://campus:p%40ss@db.example.edu:6543/glimmer?sslmode=require',
            sqlite_path='/tmp/unused.sqlite3',
            conn_max_age=60,
            conn_health_checks=True,
        )['default']
        self.assertEqual(remote['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(remote['NAME'], 'glimmer')
        self.assertEqual(remote['USER'], 'campus')
        self.assertEqual(remote['PASSWORD'], 'p@ss')
        self.assertEqual(remote['HOST'], 'db.example.edu')
        self.assertEqual(remote['PORT'], 6543)
        self.assertEqual(remote['CONN_MAX_AGE'], 60)
        self.assertTrue(remote['CONN_HEALTH_CHECKS'])

    def test_cache_uses_locmem_or_redis_with_timeouts(self):
        local = cache_config('', key_prefix='local')['default']
        self.assertEqual(local['BACKEND'], 'django.core.cache.backends.locmem.LocMemCache')

        remote = cache_config(
            'redis://:secret@redis.internal:6379/2',
            key_prefix='campus-prod',
            socket_connect_timeout=4,
            socket_timeout=5,
        )['default']
        self.assertEqual(remote['BACKEND'], 'django_redis.cache.RedisCache')
        self.assertEqual(remote['KEY_PREFIX'], 'campus-prod')
        self.assertEqual(remote['OPTIONS']['SOCKET_CONNECT_TIMEOUT'], 4)
        self.assertEqual(remote['OPTIONS']['SOCKET_TIMEOUT'], 5)
        self.assertFalse(remote['OPTIONS']['IGNORE_EXCEPTIONS'])

    def test_cache_rejects_non_redis_urls(self):
        for value in ('memcached://cache:11211', 'redis:///missing-host'):
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    cache_config(value)

    def test_storage_supports_local_and_s3_compatible_backends(self):
        local = storage_config('filesystem')
        self.assertEqual(local['default']['BACKEND'], 'django.core.files.storage.FileSystemStorage')

        remote = storage_config(
            's3',
            s3_options={'bucket_name': 'campus-glimmer', 'endpoint_url': 'http://minio:9000'},
        )
        self.assertEqual(
            remote['default']['BACKEND'],
            'campus_glimmer.storage.PublicEndpointS3Storage',
        )
        self.assertEqual(remote['default']['OPTIONS']['bucket_name'], 'campus-glimmer')

        with self.assertRaises(ImproperlyConfigured):
            storage_config('s3', s3_options={})
        with self.assertRaises(ImproperlyConfigured):
            storage_config('ftp')

    def test_production_validation_is_explicit_and_fails_closed(self):
        valid = {
            'debug': False,
            'required': True,
            'database_url': 'postgresql://campus:secret@db:5432/glimmer',
            'cache_url': 'redis://:secret@redis:6379/1',
            'storage_backend': 's3',
            'secret_key': 'random-production-secret-key-with-32-chars',
        }
        validate_production_services(**valid)
        validate_production_services(**{**valid, 'required': False})
        validate_production_services(**{**valid, 'debug': True})

        invalid_cases = (
            {'secret_key': 'short'},
            {'secret_key': 'CHANGE_ME_TO_A_RANDOM_VALUE_WITH_AT_LEAST_32_CHARS'},
            {'database_url': 'sqlite:///tmp/db.sqlite3'},
            {'database_url': 'postgresql://campus:CHANGE_ME@db:5432/glimmer'},
            {'cache_url': 'locmem://'},
            {'storage_backend': 'filesystem'},
        )
        for changes in invalid_cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ImproperlyConfigured):
                    validate_production_services(**{**valid, **changes})

    def test_s3_urls_can_be_signed_against_a_browser_facing_endpoint(self):
        storage = PublicEndpointS3Storage(
            bucket_name='campus-glimmer',
            region_name='us-east-1',
            endpoint_url='http://minio:9000',
            public_endpoint_url='http://localhost:9000',
            access_key='access',
            secret_key='secret',
            addressing_style='path',
            querystring_auth=True,
        )
        client = Mock()
        client.generate_presigned_url.return_value = 'http://localhost:9000/signed'
        storage._public_client = client

        self.assertEqual(storage.url('media/item.jpg'), 'http://localhost:9000/signed')
        client.generate_presigned_url.assert_called_once()
        self.assertEqual(
            client.generate_presigned_url.call_args.kwargs['Params'],
            {'Bucket': 'campus-glimmer', 'Key': 'media/item.jpg'},
        )


class BackupHelperTests(SimpleTestCase):
    def setUp(self):
        self.backup = load_backup_module()

    def test_database_parts_decode_credentials_and_keep_password_out_of_args(self):
        parts = self.backup.database_parts(
            'postgresql://campus:p%40ss@db:6543/glimmer?sslmode=require',
        )
        self.assertEqual(parts['user'], 'campus')
        self.assertEqual(parts['password'], 'p@ss')
        self.assertEqual(parts['port'], '6543')
        self.assertEqual(parts['sslmode'], 'require')
        with self.assertRaises(ValueError):
            self.backup.database_parts('sqlite:///tmp/db.sqlite3')

    def test_backup_rejects_a_pg_dump_major_mismatch(self):
        database = {
            'host': 'db',
            'port': '5432',
            'user': 'campus',
            'password': 'secret',
            'name': 'glimmer',
            'sslmode': '',
        }
        with patch.object(self.backup, 'postgres_client_major', return_value=17), patch.object(
            self.backup, 'postgres_server_major', return_value=16,
        ):
            with self.assertRaisesRegex(RuntimeError, '主版本 17.*主版本 16'):
                self.backup.ensure_postgres_client_compatibility(database)

    def test_backup_accepts_matching_postgresql_major_versions(self):
        database = {
            'host': 'db',
            'port': '5432',
            'user': 'campus',
            'password': 'secret',
            'name': 'glimmer',
            'sslmode': '',
        }
        with patch.object(self.backup, 'postgres_client_major', return_value=16), patch.object(
            self.backup, 'postgres_server_major', return_value=16,
        ):
            self.backup.ensure_postgres_client_compatibility(database)

    def test_object_backup_prefix_is_directory_scoped(self):
        with tempfile.TemporaryDirectory() as temporary:
            client = Mock()
            client.get_paginator.return_value.paginate.return_value = []

            self.backup._download_objects(
                client, 'campus-glimmer', 'media', Path(temporary),
            )

            client.get_paginator.return_value.paginate.assert_called_once_with(
                Bucket='campus-glimmer', Prefix='media/',
            )

    def test_verify_backup_checks_database_and_object_checksums(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'objects').mkdir()
            database = b'postgres custom dump'
            object_bytes = b'campus image bytes'
            (root / 'database.dump').write_bytes(database)
            (root / 'objects' / '00000000-photo.jpg').write_bytes(object_bytes)
            manifest = {
                'format_version': self.backup.MANIFEST_FORMAT,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'database': {
                    'file': 'database.dump',
                    'size': len(database),
                    'sha256': hashlib.sha256(database).hexdigest(),
                },
                'object_storage': {
                    'count': 1,
                    'objects': [{
                        'key': 'media/photo.jpg',
                        'file': 'objects/00000000-photo.jpg',
                        'size': len(object_bytes),
                        'sha256': hashlib.sha256(object_bytes).hexdigest(),
                    }],
                },
            }
            (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            self.backup.verify_backup(root)

            (root / 'objects' / '00000000-photo.jpg').write_bytes(b'x' * len(object_bytes))
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                self.backup.verify_backup(root)

    def test_restore_objects_uploads_to_original_keys_without_deleting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'objects').mkdir()
            object_bytes = b'image'
            (root / 'objects' / '00000000-photo.jpg').write_bytes(object_bytes)
            manifest = {
                'format_version': self.backup.MANIFEST_FORMAT,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'database': {'file': 'database.dump', 'size': 0, 'sha256': hashlib.sha256(b'').hexdigest()},
                'object_storage': {
                    'bucket': 'source-bucket',
                    'count': 1,
                    'objects': [{
                        'key': 'media/photo.jpg',
                        'file': 'objects/00000000-photo.jpg',
                        'size': len(object_bytes),
                        'sha256': hashlib.sha256(object_bytes).hexdigest(),
                    }],
                },
            }
            (root / 'database.dump').write_bytes(b'')
            (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            client = Mock()

            count = self.backup.restore_objects(root, bucket='restore-bucket', client=client)

            self.assertEqual(count, 1)
            client.upload_file.assert_called_once_with(
                str((root / 'objects' / '00000000-photo.jpg').resolve()),
                'restore-bucket',
                'media/photo.jpg',
            )
            client.delete_object.assert_not_called()

    def test_prune_only_removes_expired_valid_backup_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = datetime(2026, 10, 18, tzinfo=timezone.utc)
            old = root / 'backup-old'
            recent = root / 'backup-recent'
            unrelated = root / 'uploads'
            broken = root / 'backup-broken'
            for path, created in (
                (old, now - timedelta(days=40)),
                (recent, now - timedelta(days=2)),
            ):
                path.mkdir()
                (path / 'manifest.json').write_text(
                    json.dumps({'created_at': created.isoformat()}),
                    encoding='utf-8',
                )
            unrelated.mkdir()
            broken.mkdir()

            removed = self.backup.prune_backups(root, 30, now=now)

            self.assertEqual(removed, [old.resolve()])
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(broken.exists())
