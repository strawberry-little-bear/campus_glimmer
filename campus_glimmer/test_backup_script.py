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
        }
        with patch.dict(os.environ, environment), \
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