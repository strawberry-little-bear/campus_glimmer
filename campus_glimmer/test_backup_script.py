"""Offline coverage for the backup and restore sidecar (``docker/backup.py``).

A backup is only worth the disk it occupies if it can be restored, so the
script's guard rails - checksum verification, manifest path escapes, retention
pruning and the PostgreSQL client/server major version check - must never
regress silently. These tests pin that behaviour without a live PostgreSQL or
MinIO: the dump, the object download and the S3 client are all replaced, which
keeps the assertions on the contract instead of on the environment.
"""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from django.test import SimpleTestCase

from .test_deployment import load_backup_module


class BackupScriptTests(SimpleTestCase):
    def setUp(self):
        self.backup = load_backup_module()
        self.database = {
            'host': 'db',
            'port': '5432',
            'user': 'campus',
            'password': 'secret',
            'name': 'glimmer',
            'sslmode': '',
        }

    def _write_backup(self, root, *, database_bytes=b'pg custom dump', object_bytes=b'photo'):
        """Write a consistent backup directory and return its manifest."""
        root = Path(root)
        (root / 'objects').mkdir(parents=True, exist_ok=True)
        (root / 'database.dump').write_bytes(database_bytes)
        (root / 'objects' / '00000000-photo.jpg').write_bytes(object_bytes)
        manifest = {
            'format_version': self.backup.MANIFEST_FORMAT,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'database': {
                'host': 'db',
                'port': '5432',
                'name': 'glimmer',
                'file': 'database.dump',
                'size': len(database_bytes),
                'sha256': hashlib.sha256(database_bytes).hexdigest(),
            },
            'object_storage': {
                'bucket': 'campus-glimmer',
                'prefix': 'media',
                'count': 1,
                'objects': [{
                    'key': 'media/photo.jpg',
                    'file': 'objects/00000000-photo.jpg',
                    'size': len(object_bytes),
                    'sha256': hashlib.sha256(object_bytes).hexdigest(),
                    'etag': 'etag',
                    'last_modified': None,
                }],
            },
        }
        (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return manifest

    def _write_prune_candidate(self, root, name, created):
        path = Path(root) / name
        path.mkdir()
        (path / 'manifest.json').write_text(
            json.dumps({'created_at': created.isoformat()}),
            encoding='utf-8',
        )
        return path


class DatabaseUrlParsingTests(BackupScriptTests):
    def test_rejects_urls_without_a_host_or_a_database_name(self):
        for url in ('postgres://campus@db:5432', 'postgres://campus@db:5432/', 'postgres:///glimmer'):
            with self.assertRaises(ValueError):
                self.backup.database_parts(url)

    def test_defaults_the_port_and_leaves_sslmode_empty(self):
        parts = self.backup.database_parts('postgres://campus@db/glimmer')
        self.assertEqual(parts['port'], '5432')
        self.assertEqual(parts['name'], 'glimmer')
        self.assertEqual(parts['sslmode'], '')


class ChecksumTests(BackupScriptTests):
    def test_sha256_file_matches_hashlib_across_chunk_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'payload.bin'
            payload = b'c' * (1024 * 1024 + 7)
            path.write_bytes(payload)
            self.assertEqual(
                self.backup.sha256_file(path),
                hashlib.sha256(payload).hexdigest(),
            )


class ManifestPathTests(BackupScriptTests):
    def test_rejects_paths_that_escape_the_backup_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ('../escape.dump', '/etc/passwd', '', 'objects/../../escape'):
                with self.assertRaisesRegex(ValueError, '非法路径'):
                    self.backup._safe_backup_path(root, relative)

    def test_accepts_paths_inside_the_backup_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resolved = self.backup._safe_backup_path(root, 'objects/00000000-photo.jpg')
            self.assertTrue(resolved.is_relative_to(root.resolve()))


class VerifyBackupTests(BackupScriptTests):
    def test_accepts_a_consistent_backup_and_returns_the_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_backup(root)
            manifest = self.backup.verify_backup(root)
            self.assertEqual(manifest['object_storage']['count'], 1)

    def test_reports_a_database_size_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_backup(root)
            (root / 'database.dump').write_bytes(b'truncated')
            with self.assertRaisesRegex(ValueError, 'database.dump 大小不匹配'):
                self.backup.verify_backup(root)

    def test_reports_an_unsupported_manifest_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_backup(root)
            manifest['format_version'] = 99
            (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '版本不受支持'):
                self.backup.verify_backup(root)

    def test_reports_an_unreadable_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_backup(root)
            (root / 'manifest.json').write_text('{ not json', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '无法读取备份清单'):
                self.backup.verify_backup(root)

            (root / 'manifest.json').unlink()
            with self.assertRaisesRegex(ValueError, '无法读取备份清单'):
                self.backup.verify_backup(root)

    def test_reports_paths_that_point_outside_the_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_backup(root)
            manifest['database']['file'] = '../outside.dump'
            (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'database.dump 缺失或路径非法'):
                self.backup.verify_backup(root)

            manifest = self._write_backup(root)
            manifest['object_storage']['objects'][0]['file'] = '../../outside.jpg'
            (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '对象缺失或路径非法'):
                self.backup.verify_backup(root)

    def test_reports_an_object_count_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self._write_backup(root)
            manifest['object_storage']['count'] = 2
            (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, '对象数量与清单不一致'):
                self.backup.verify_backup(root)

    def test_collects_every_problem_into_one_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_backup(root)
            (root / 'database.dump').write_bytes(b'pg custom duXX')
            (root / 'objects' / '00000000-photo.jpg').write_bytes(b'XXXXX')
            with self.assertRaises(ValueError) as caught:
                self.backup.verify_backup(root)
            message = str(caught.exception)
            self.assertIn('database.dump SHA-256 不匹配', message)
            self.assertIn('对象 SHA-256 不匹配', message)
            self.assertEqual(message.count('；'), 1)


class PruneBackupsTests(BackupScriptTests):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 24, tzinfo=timezone.utc)

    def test_retention_below_one_day_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = self._write_prune_candidate(root, 'backup-old', self.now - timedelta(days=400))
            self.assertEqual(self.backup.prune_backups(root, 0, now=self.now), [])
            self.assertTrue(old.exists())

    def test_a_missing_root_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / 'absent'
            self.assertEqual(self.backup.prune_backups(missing, 30), [])

    def test_only_expired_backup_directories_are_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = self._write_prune_candidate(root, 'backup-old', self.now - timedelta(days=40))
            recent = self._write_prune_candidate(root, 'backup-recent', self.now - timedelta(days=2))
            unrelated = root / 'uploads'
            unrelated.mkdir()
            plain_file = root / 'backup-notes.txt'
            plain_file.write_text('not a directory', encoding='utf-8')

            removed = self.backup.prune_backups(root, 30, now=self.now)

            self.assertEqual(removed, [old.resolve()])
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(plain_file.exists())

    def test_a_naive_manifest_timestamp_is_treated_as_utc(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            naive = self._write_prune_candidate(
                root, 'backup-naive', self.now - timedelta(days=40),
            )
            (naive / 'manifest.json').write_text(
                json.dumps({'created_at': '2026-09-14T12:00:00'}),
                encoding='utf-8',
            )
            removed = self.backup.prune_backups(root, 30, now=self.now)
            self.assertEqual(removed, [naive.resolve()])

    def test_an_unparseable_manifest_is_skipped_without_blocking_others(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broken = root / 'backup-broken'
            broken.mkdir()
            (broken / 'manifest.json').write_text('{ not json', encoding='utf-8')
            mistyped = root / 'backup-mistyped'
            mistyped.mkdir()
            (mistyped / 'manifest.json').write_text(
                json.dumps({'created_at': 12345}), encoding='utf-8',
            )
            empty = root / 'backup-empty'
            empty.mkdir()
            old = self._write_prune_candidate(root, 'backup-old', self.now - timedelta(days=90))

            removed = self.backup.prune_backups(root, 30, now=self.now)

            self.assertEqual(removed, [old.resolve()])
            for kept in (broken, mistyped, empty):
                self.assertTrue(kept.exists())


class ObjectPrefixTests(BackupScriptTests):
    def test_prefixes_are_normalised_to_a_directory_scope(self):
        cases = {
            '': '',
            '/': '',
            'media': 'media/',
            'media/': 'media/',
            '/media/': 'media/',
        }
        for raw, expected in cases.items():
            with self.subTest(prefix=raw):
                self.assertEqual(self.backup.object_listing_prefix(raw), expected)


class PostgresVersionGuardTests(BackupScriptTests):
    def test_client_major_is_read_from_pg_dump(self):
        with patch.object(
            self.backup.subprocess, 'run',
            return_value=Mock(stdout='pg_dump (PostgreSQL) 16.15', stderr=''),
        ) as run:
            self.assertEqual(self.backup.postgres_client_major(), 16)
        self.assertEqual(run.call_args.args[0], ['pg_dump', '--version'])

    def test_client_major_failure_is_explicit(self):
        with patch.object(
            self.backup.subprocess, 'run',
            return_value=Mock(stdout='pg_dump (PostgreSQL) unknown', stderr=''),
        ):
            with self.assertRaisesRegex(RuntimeError, '无法识别 pg_dump 主版本'):
                self.backup.postgres_client_major()

    def test_server_major_is_read_without_leaking_the_password(self):
        result = Mock(stdout=' 160005 \n', stderr='')
        with patch.object(self.backup.subprocess, 'run', return_value=result) as run:
            self.assertEqual(self.backup.postgres_server_major(self.database), 16)
        command, kwargs = run.call_args
        command = list(command[0])
        self.assertEqual(command[0], 'psql')
        self.assertIn('SHOW server_version_num;', command)
        self.assertNotIn('secret', command)
        self.assertEqual(kwargs['env']['PGPASSWORD'], 'secret')

    def test_server_major_failure_is_explicit(self):
        with patch.object(
            self.backup.subprocess, 'run',
            return_value=Mock(stdout='not-a-number', stderr=''),
        ):
            with self.assertRaisesRegex(RuntimeError, '无法识别 PostgreSQL 服务端主版本'):
                self.backup.postgres_server_major(self.database)


class DumpCommandTests(BackupScriptTests):
    def test_pg_dump_keeps_the_password_in_the_environment_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'database.dump'
            with patch.object(self.backup, 'ensure_postgres_client_compatibility') as compat, \
                    patch.object(self.backup.subprocess, 'run') as run:
                database = self.backup._dump_database(
                    'postgresql://campus:p%40ss@db:6543/glimmer?sslmode=require',
                    destination,
                )
            compat.assert_called_once_with(database)
            command, kwargs = run.call_args
            command = list(command[0])
            self.assertEqual(command[:3], ['pg_dump', '--format=custom', '--no-owner'])
            self.assertIn('--no-acl', command)
            self.assertIn(str(destination), command)
            self.assertNotIn('p@ss', command)
            self.assertNotIn('--password', command)
            self.assertEqual(kwargs['env']['PGPASSWORD'], 'p@ss')
            self.assertEqual(kwargs['env']['PGSSLMODE'], 'require')
            self.assertTrue(kwargs['check'])


class CreateBackupTests(BackupScriptTests):
    URL = 'postgres://campus:secret@db:5432/glimmer'

    def _patch_steps(self, *, dump_error=None, download_error=None, objects=None):
        def fake_dump(database_url, destination):
            if dump_error is not None:
                raise dump_error
            Path(destination).write_bytes(b'pg custom dump')
            return self.database

        def fake_download(client, bucket, prefix, objects_dir):
            if download_error is not None:
                raise download_error
            entries = []
            for index, (key, payload) in enumerate(
                objects or [('media/photo.jpg', b'photo')],
            ):
                name = f'{index:08d}-{Path(key).name}'
                (Path(objects_dir) / name).write_bytes(payload)
                entries.append({
                    'key': key,
                    'file': f'objects/{name}',
                    'size': len(payload),
                    'sha256': hashlib.sha256(payload).hexdigest(),
                    'etag': '',
                    'last_modified': None,
                })
            return entries

        return (
            patch.object(self.backup, '_dump_database', side_effect=fake_dump),
            patch.object(self.backup, '_download_objects', side_effect=fake_download),
        )

    def test_requires_a_database_url_and_a_bucket(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'DJANGO_DATABASE_URL'):
                self.backup.create_backup(temporary, database_url='', bucket='b')
            with self.assertRaisesRegex(ValueError, 'DJANGO_S3_BUCKET'):
                self.backup.create_backup(temporary, database_url=self.URL, bucket='')
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_creates_one_verified_backup_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dump_patch, download_patch = self._patch_steps()
            with dump_patch, download_patch:
                final = self.backup.create_backup(
                    root, database_url=self.URL, bucket='campus-glimmer', prefix='media',
                    client=Mock(),
                )
            self.assertRegex(final.name, r'^backup-\d{8}T\d{6}Z$')
            self.assertEqual([entry.name for entry in root.iterdir()], [final.name])
            manifest = json.loads((final / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['object_storage']['bucket'], 'campus-glimmer')
            self.assertEqual(manifest['object_storage']['prefix'], 'media')
            self.assertEqual(manifest['object_storage']['count'], 1)
            self.assertEqual(manifest['database']['name'], 'glimmer')
            self.assertEqual(manifest['database']['size'], len(b'pg custom dump'))
            self.assertNotIn('secret', json.dumps(manifest))

    def test_a_failed_dump_leaves_no_partial_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dump_patch, download_patch = self._patch_steps(
                dump_error=RuntimeError('pg_dump 主版本不一致'),
            )
            with dump_patch, download_patch:
                with self.assertRaisesRegex(RuntimeError, '主版本不一致'):
                    self.backup.create_backup(
                        root, database_url=self.URL, bucket='campus-glimmer', client=Mock(),
                    )
            self.assertEqual(list(root.iterdir()), [])

    def test_a_failed_download_leaves_no_partial_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dump_patch, download_patch = self._patch_steps(
                download_error=RuntimeError('bucket unreachable'),
            )
            with dump_patch, download_patch:
                with self.assertRaisesRegex(RuntimeError, 'bucket unreachable'):
                    self.backup.create_backup(
                        root, database_url=self.URL, bucket='campus-glimmer', client=Mock(),
                    )
            self.assertEqual(list(root.iterdir()), [])

    def test_a_duplicate_timestamp_still_gets_its_own_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stamp = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
            existing = root / 'backup-20261024T120000Z'
            existing.mkdir()
            dump_patch, download_patch = self._patch_steps()
            with dump_patch, download_patch, patch.object(
                self.backup, 'datetime',
            ) as fake_datetime:
                fake_datetime.now.return_value = stamp
                fake_datetime.timezone.utc = timezone.utc
                final = self.backup.create_backup(
                    root, database_url=self.URL, bucket='campus-glimmer', client=Mock(),
                )
            self.assertRegex(final.name, r'^backup-20261024T120000Z-[0-9a-f]{8}$')
            self.assertNotEqual(final, existing)
            self.assertEqual(len(list(root.iterdir())), 2)


class RunOnceTests(BackupScriptTests):
    def test_run_once_prunes_after_the_backup_using_environment_values(self):
        final = Path('/backups/backup-20261024T120000Z')
        environment = {
            'BACKUP_DIR': '/tmp/backups',
            'BACKUP_RETENTION_DAYS': '7',
            'DJANGO_DATABASE_URL': 'postgres://campus@db/glimmer',
            'DJANGO_S3_BUCKET': 'campus-glimmer',
            'DJANGO_S3_LOCATION': 'media',
            **dict.fromkeys(self.backup.OFFSITE_ENV, ''),
        }
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(self.backup, 'create_backup', return_value=final) as create, \
                patch.object(self.backup, 'prune_backups', return_value=[final]) as prune:
            self.backup.run_once()
        create.assert_called_once_with(
            '/tmp/backups',
            database_url='postgres://campus@db/glimmer',
            bucket='campus-glimmer',
            prefix='media',
        )
        prune.assert_called_once_with('/tmp/backups', 7)

    def test_run_once_falls_back_to_the_documented_defaults(self):
        # clear=True 同时意味着没有任何 BACKUP_OFFSITE_*，即异地镜像保持关闭。
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(self.backup, 'create_backup') as create, \
                patch.object(self.backup, 'prune_backups', return_value=[]) as prune:
            self.backup.run_once()
        create.assert_called_once_with(
            '/backups',
            database_url='',
            bucket='',
            prefix='',
        )
        prune.assert_called_once_with('/backups', 14)


class _Body:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


class FakeOffsiteClient:
    """内存里的假 S3，只实现异地镜像真正用到的那几个调用。

    刻意不模仿 boto3 的全部行为：这里要验的是备份脚本的契约（传了什么、读回了
    什么、删了什么），不是 SDK 的语义。
    """

    def __init__(self, objects=None, *, corrupt=(), drop=()):
        self.objects = dict(objects or {})
        self.uploads = []
        self.deleted = []
        self.reads = []
        self.corrupt = set(corrupt)
        self.drop = set(drop)

    def upload_file(self, path, bucket, key):
        self.uploads.append(key)
        self.objects[key] = Path(path).read_bytes()

    def get_object(self, Bucket, Key):
        self.reads.append(Key)
        if Key not in self.objects:
            # 与真实 S3 一致：不存在的 key 是 ClientError，不是 KeyError。
            raise ClientError(
                {'Error': {'Code': 'NoSuchKey', 'Message': 'missing'}}, 'GetObject')
        payload = self.objects[Key]
        if Key in self.corrupt:
            payload = payload[:-1]
        return {'Body': _Body(payload)}

    def get_paginator(self, name):
        client = self

        class _Paginator:
            def paginate(self, Bucket, Prefix):
                keys = sorted(
                    key for key in client.objects
                    if key.startswith(Prefix) and key not in client.drop
                )
                yield {'Contents': [{'Key': key} for key in keys]}

        return _Paginator()

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


class OffsiteConfigTests(BackupScriptTests):
    """异地目标要么完整，要么明确没开；「配了一半」必须当场报错。"""

    BASE = {
        'BACKUP_OFFSITE_ENDPOINT_URL': 'https://offsite.example.com',
        'BACKUP_OFFSITE_BUCKET': 'glimmer-offsite',
        'BACKUP_OFFSITE_ACCESS_KEY_ID': 'key',
        'BACKUP_OFFSITE_SECRET_ACCESS_KEY': 'secret',
    }

    def test_returns_none_when_nothing_is_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.backup.offsite_config())

    def test_reads_a_complete_configuration(self):
        with patch.dict(os.environ, dict(self.BASE, BACKUP_OFFSITE_PREFIX='dr/',
                                         BACKUP_OFFSITE_RETENTION_DAYS='30'), clear=True):
            config = self.backup.offsite_config()
        self.assertEqual(config['endpoint_url'], 'https://offsite.example.com')
        self.assertEqual(config['bucket'], 'glimmer-offsite')
        self.assertEqual(config['region'], 'us-east-1')
        self.assertEqual(config['prefix'], 'dr/')
        self.assertEqual(config['retention_days'], 30)

    def test_an_endpoint_without_a_bucket_is_an_error(self):
        with patch.dict(os.environ, {'BACKUP_OFFSITE_ENDPOINT_URL': 'https://x'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'BACKUP_OFFSITE_BUCKET'):
                self.backup.offsite_config()

    def test_missing_credentials_are_an_error(self):
        environment = {'BACKUP_OFFSITE_ENDPOINT_URL': 'https://x',
                       'BACKUP_OFFSITE_BUCKET': 'b'}
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, 'ACCESS_KEY_ID'):
                self.backup.offsite_config()

    def test_stray_keys_without_an_endpoint_are_an_error(self):
        # 只填了桶和账号却漏了端点，镜像会静默不执行——这正是最危险的那种配置。
        environment = {'BACKUP_OFFSITE_BUCKET': 'b',
                       'BACKUP_OFFSITE_ACCESS_KEY_ID': 'k',
                       'BACKUP_OFFSITE_SECRET_ACCESS_KEY': 's'}
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, 'BACKUP_OFFSITE_ENDPOINT_URL'):
                self.backup.offsite_config()

    def test_a_non_numeric_retention_is_an_error(self):
        environment = dict(self.BASE, BACKUP_OFFSITE_RETENTION_DAYS='two weeks')
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, 'BACKUP_OFFSITE_RETENTION_DAYS'):
                self.backup.offsite_config()

    def test_retention_falls_back_to_the_local_retention(self):
        with patch.dict(os.environ, dict(self.BASE, BACKUP_RETENTION_DAYS='3'), clear=True):
            self.assertEqual(self.backup.offsite_config()['retention_days'], 3)


class MirrorBackupTests(BackupScriptTests):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.backup_dir = self.root / 'backup-20261024T120000Z'
        self.manifest = self._write_backup(self.backup_dir)
        self.config = {
            'endpoint_url': 'https://offsite.example.com',
            'bucket': 'glimmer-offsite',
            'access_key_id': 'key',
            'secret_access_key': 'secret',
            'region': 'us-east-1',
            'prefix': 'dr/',
            'retention_days': 14,
        }

    def _keys(self, client):
        return set(client.uploads)

    def test_uploads_every_verified_file_under_the_archive_name(self):
        client = FakeOffsiteClient()
        result = self.backup.mirror_backup(
            self.backup_dir, config=self.config, client=client)
        self.assertEqual(
            self._keys(client),
            {
                'dr/backup-20261024T120000Z/database.dump',
                'dr/backup-20261024T120000Z/manifest.json',
                'dr/backup-20261024T120000Z/objects/00000000-photo.jpg',
            },
        )
        self.assertEqual(result['keys'], 3)
        self.assertEqual(result['name'], 'backup-20261024T120000Z')
        self.assertGreater(result['bytes'], 0)

    def test_the_remote_copy_is_read_back_and_compared_byte_for_byte(self):
        client = FakeOffsiteClient()
        self.backup.mirror_backup(self.backup_dir, config=self.config, client=client)
        for key in client.uploads:
            self.assertEqual(
                client.objects[key],
                (self.backup_dir / key.split('/', 2)[2]).read_bytes(),
                '异地副本与本地归档不一致：' + key,
            )

    def test_a_truncated_upload_fails_the_backup(self):
        client = FakeOffsiteClient(
            corrupt={'dr/backup-20261024T120000Z/database.dump'})
        with self.assertRaisesRegex(ValueError, '异地副本校验失败'):
            self.backup.mirror_backup(self.backup_dir, config=self.config, client=client)

    def test_a_missing_remote_key_fails_the_backup(self):
        client = FakeOffsiteClient(
            drop={'dr/backup-20261024T120000Z/manifest.json'})
        with self.assertRaisesRegex(ValueError, '缺少'):
            self.backup.mirror_backup(self.backup_dir, config=self.config, client=client)

    def test_an_unexpected_remote_key_fails_the_backup(self):
        client = FakeOffsiteClient(
            objects={'dr/backup-20261024T120000Z/rogue': b'x'})
        with self.assertRaisesRegex(ValueError, '多出'):
            self.backup.mirror_backup(self.backup_dir, config=self.config, client=client)

    def test_an_unverifiable_archive_is_never_uploaded(self):
        (self.backup_dir / 'database.dump').write_bytes(b'tampered')
        client = FakeOffsiteClient()
        with self.assertRaises(ValueError):
            self.backup.mirror_backup(self.backup_dir, config=self.config, client=client)
        self.assertEqual(client.uploads, [])


class PruneOffsiteTests(BackupScriptTests):
    CONFIG = {
        'endpoint_url': 'https://offsite.example.com',
        'bucket': 'glimmer-offsite',
        'access_key_id': 'key',
        'secret_access_key': 'secret',
        'region': 'us-east-1',
        'prefix': 'dr/',
        'retention_days': 14,
    }

    def _archive(self, name, created):
        return (
            f'dr/{name}/manifest.json',
            json.dumps({'format_version': 1, 'created_at': created}).encode('utf-8'),
        )

    def test_only_expired_archives_are_deleted(self):
        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        objects = dict([
            self._archive('backup-old', '2026-10-01T00:00:00+00:00'),
            ('dr/backup-old/database.dump', b'dump'),
            self._archive('backup-new', '2026-10-20T00:00:00+00:00'),
            ('dr/backup-new/database.dump', b'dump'),
        ])
        client = FakeOffsiteClient(objects)
        removed = self.backup.prune_offsite(
            self.CONFIG, 14, now=now, client=client)
        self.assertEqual(removed, ['backup-old'])
        self.assertEqual(client.deleted, [
            'dr/backup-old/database.dump',
            'dr/backup-old/manifest.json',
        ])
        self.assertIn('dr/backup-new/database.dump', client.objects)

    def test_a_naive_manifest_timestamp_is_treated_as_utc(self):
        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        client = FakeOffsiteClient(dict([
            self._archive('backup-old', '2026-10-01T00:00:00'),
            ('dr/backup-old/database.dump', b'dump'),
        ]))
        self.assertEqual(
            self.backup.prune_offsite(self.CONFIG, 14, now=now, client=client),
            ['backup-old'],
        )

    def test_an_archive_without_a_manifest_is_kept(self):
        # 没有可判定的创建时间就不删：凭 key 名猜时间去删数据比多留一天危险。
        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        client = FakeOffsiteClient({'dr/backup-old/database.dump': b'dump'})
        self.assertEqual(
            self.backup.prune_offsite(self.CONFIG, 14, now=now, client=client), [])
        self.assertEqual(client.deleted, [])
        # 没有 manifest 就连读都不该去读：创建时间无从判断，就不能凭猜去删。
        self.assertEqual(client.reads, [])

    def test_an_unreadable_manifest_does_not_block_the_others(self):
        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        objects = dict([
            ('dr/backup-broken/manifest.json', b'not json'),
            ('dr/backup-broken/database.dump', b'dump'),
            self._archive('backup-old', '2026-10-01T00:00:00+00:00'),
            ('dr/backup-old/database.dump', b'dump'),
        ])
        client = FakeOffsiteClient(objects)
        self.assertEqual(
            self.backup.prune_offsite(self.CONFIG, 14, now=now, client=client),
            ['backup-old'],
        )

    def test_keys_outside_the_prefix_are_ignored(self):
        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        client = FakeOffsiteClient(dict([
            self._archive('backup-old', '2026-10-01T00:00:00+00:00'),
            ('dr/backup-old/database.dump', b'dump'),
            ('media/00000000-photo.jpg', b'photo'),
        ]))
        self.assertEqual(
            self.backup.prune_offsite(self.CONFIG, 14, now=now, client=client),
            ['backup-old'],
        )
        self.assertEqual(client.deleted, [
            'dr/backup-old/database.dump', 'dr/backup-old/manifest.json',
        ])

    def test_a_retention_below_one_day_deletes_nothing(self):
        # 保留期写成 0 是配置事故，此时"全部过期"会一次性清空异地备份，
        # 所以必须整棵跳过，而不是把 cutoff 当成现在。
        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        client = FakeOffsiteClient(dict([
            self._archive('backup-old', '2026-10-01T00:00:00+00:00'),
            ('dr/backup-old/database.dump', b'dump'),
        ]))
        self.assertEqual(self.backup.prune_offsite(self.CONFIG, 0, now=now, client=client), [])
        self.assertEqual(client.deleted, [])
        self.assertEqual(sorted(client.objects), [
            'dr/backup-old/database.dump', 'dr/backup-old/manifest.json',
        ])


class RunOnceOffsiteTests(BackupScriptTests):
    FINAL = Path('/backups/backup-20261024T120000Z')
    OFFSITE = {
        'BACKUP_OFFSITE_ENDPOINT_URL': 'https://offsite.example.com',
        'BACKUP_OFFSITE_BUCKET': 'glimmer-offsite',
        'BACKUP_OFFSITE_ACCESS_KEY_ID': 'key',
        'BACKUP_OFFSITE_SECRET_ACCESS_KEY': 'secret',
    }
    BASE = {
        'BACKUP_DIR': '/tmp/backups',
        'BACKUP_RETENTION_DAYS': '7',
        'DJANGO_DATABASE_URL': 'postgres://campus@db/glimmer',
        'DJANGO_S3_BUCKET': 'campus-glimmer',
        'DJANGO_S3_LOCATION': 'media',
    }

    def _offsite_empty(self):
        return dict.fromkeys(self.backup.OFFSITE_ENV, '')

    def test_run_once_mirrors_the_new_archive_and_prunes_the_remote_copy(self):
        environment = dict(self.BASE, **self.OFFSITE,
                           BACKUP_OFFSITE_RETENTION_DAYS='30')
        with patch.dict(os.environ, environment, clear=True),                 patch.object(self.backup, 'create_backup', return_value=self.FINAL),                 patch.object(self.backup, 'prune_backups', return_value=[]),                 patch.object(self.backup, 'mirror_backup',
                             return_value={'name': 'backup-20261024T120000Z',
                                           'bucket': 'glimmer-offsite',
                                           'prefix': 'dr/backup-20261024T120000Z/',
                                           'keys': 3, 'bytes': 12}) as mirror,                 patch.object(self.backup, 'prune_offsite',
                             return_value=['backup-old']) as prune_remote:
            self.backup.run_once()
        self.assertEqual(mirror.call_count, 1)
        config = mirror.call_args.kwargs['config']
        self.assertEqual(config['bucket'], 'glimmer-offsite')
        self.assertEqual(config['retention_days'], 30)
        self.assertEqual(mirror.call_args.args[0], self.FINAL)
        prune_remote.assert_called_once_with(config, 30)

    def test_run_once_skips_mirroring_when_no_offsite_target_is_configured(self):
        environment = dict(self.BASE, **self._offsite_empty())
        with patch.dict(os.environ, environment, clear=True),                 patch.object(self.backup, 'create_backup', return_value=self.FINAL),                 patch.object(self.backup, 'prune_backups', return_value=[]),                 patch.object(self.backup, 'mirror_backup') as mirror:
            self.backup.run_once()
        mirror.assert_not_called()

    def test_a_failed_mirror_propagates_so_the_sidecar_reports_a_failure(self):
        # 备份没到异地就是没备份成功，不能只在本机留下一个"看起来完成"的归档。
        environment = dict(self.BASE, **self.OFFSITE)
        with patch.dict(os.environ, environment, clear=True),                 patch.object(self.backup, 'create_backup', return_value=self.FINAL),                 patch.object(self.backup, 'prune_backups', return_value=[]),                 patch.object(self.backup, 'mirror_backup',
                             side_effect=ValueError('异地副本校验失败')):
            with self.assertRaisesRegex(ValueError, '异地副本校验失败'):
                self.backup.run_once()
