"""Create a portable PostgreSQL + S3/MinIO backup.

The backup directory contains ``database.dump`` in PostgreSQL custom format,
a byte-for-byte object mirror under ``objects/``, and ``manifest.json`` with
the original object keys and SHA-256 checksums. The script intentionally uses
``pg_dump`` instead of Django serialization so identifiers, indexes and
constraints are restored together with the data.

The same archive can additionally be mirrored to a second, independently
configured S3-compatible bucket, so a dead host does not take the business
volumes and the only backup with it. That mirror is verified by reading every
object back and comparing SHA-256, because a truncated upload still looks like
a complete key in a listing.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


BACKUP_PREFIX = 'backup-'
MANIFEST_FORMAT = 1

# 异地镜像的配置项。它们刻意与 DJANGO_S3_* 分开：业务存储和备份容灾必须能指向
# 不同的主机、不同的账号，否则主机一坏两边一起没，"异地"只是换个目录名。
OFFSITE_ENV = (
    'BACKUP_OFFSITE_ENDPOINT_URL',
    'BACKUP_OFFSITE_BUCKET',
    'BACKUP_OFFSITE_ACCESS_KEY_ID',
    'BACKUP_OFFSITE_SECRET_ACCESS_KEY',
    'BACKUP_OFFSITE_REGION',
    'BACKUP_OFFSITE_PREFIX',
    'BACKUP_OFFSITE_RETENTION_DAYS',
)


def _env(name, default=''):
    return os.getenv(name, default).strip()


def database_parts(database_url):
    """Return connection parts without putting the password in process args."""
    parsed = urlparse(database_url)
    if parsed.scheme.lower() not in {'postgres', 'postgresql'}:
        raise ValueError('DJANGO_DATABASE_URL 必须指向 PostgreSQL。')
    if not parsed.hostname or not parsed.path.strip('/'):
        raise ValueError('DJANGO_DATABASE_URL 缺少数据库主机或库名。')
    query = parse_qs(parsed.query)
    return {
        'host': parsed.hostname,
        'port': str(parsed.port or 5432),
        'user': unquote(parsed.username or ''),
        'password': unquote(parsed.password or ''),
        'name': unquote(parsed.path.lstrip('/')),
        'sslmode': (query.get('sslmode') or [''])[0],
    }


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _database_env(database):
    env = os.environ.copy()
    env['PGPASSWORD'] = database['password']
    if database['sslmode']:
        env['PGSSLMODE'] = database['sslmode']
    return env


def postgres_client_major():
    """Return the installed pg_dump major version."""
    result = subprocess.run(
        ['pg_dump', '--version'],
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout or result.stderr
    match = re.search(r'\(PostgreSQL\)\s+(\d+)', output)
    if not match:
        raise RuntimeError('无法识别 pg_dump 主版本。')
    return int(match.group(1))


def postgres_server_major(database):
    """Return the target PostgreSQL server major without exposing credentials."""
    command = [
        'psql',
        '--no-psqlrc',
        '--no-align',
        '--tuples-only',
        '--host', database['host'],
        '--port', database['port'],
        '--username', database['user'],
        '--dbname', database['name'],
        '--command', 'SHOW server_version_num;',
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=_database_env(database),
    )
    try:
        return int(result.stdout.strip()) // 10000
    except ValueError as exc:
        raise RuntimeError('无法识别 PostgreSQL 服务端主版本。') from exc


def ensure_postgres_client_compatibility(database):
    """Refuse a dump that the target PostgreSQL major cannot reliably restore."""
    client_major = postgres_client_major()
    server_major = postgres_server_major(database)
    if client_major != server_major:
        raise RuntimeError(
            f'pg_dump 主版本 {client_major} 与 PostgreSQL 服务端主版本 '
            f'{server_major} 不一致；请固定备份镜像中的 PostgreSQL 客户端版本。',
        )


def _dump_database(database_url, destination):
    database = database_parts(database_url)
    ensure_postgres_client_compatibility(database)
    command = [
        'pg_dump',
        '--format=custom',
        '--no-owner',
        '--no-acl',
        '--host', database['host'],
        '--port', database['port'],
        '--username', database['user'],
        '--dbname', database['name'],
        '--file', str(destination),
    ]
    subprocess.run(command, check=True, env=_database_env(database))
    return database


def _s3_client():
    endpoint = _env('DJANGO_S3_ENDPOINT_URL') or None
    return boto3.session.Session().client(
        's3',
        endpoint_url=endpoint,
        region_name=_env('DJANGO_S3_REGION', 'us-east-1') or 'us-east-1',
        aws_access_key_id=_env('DJANGO_S3_ACCESS_KEY_ID') or None,
        aws_secret_access_key=_env('DJANGO_S3_SECRET_ACCESS_KEY') or None,
        config=Config(
            signature_version=_env('DJANGO_S3_SIGNATURE_VERSION', 's3v4'),
            s3={'addressing_style': _env('DJANGO_S3_ADDRESSING_STYLE', 'path')},
        ),
    )


def offsite_config():
    """读取异地镜像目标；未启用时返回 None，配了一半则当场报错。

    「以为开了异地备份」比「明确没开」危险得多：前者会让运维依据一个不存在的
    副本做恢复演练。所以只填了其中几项也必须是错误，不能静默跳过。
    """
    endpoint = _env('BACKUP_OFFSITE_ENDPOINT_URL')
    if not endpoint:
        stray = sorted(name for name in OFFSITE_ENV[1:] if _env(name))
        if stray:
            raise ValueError(
                '配置了 ' + ','.join(stray) + ' 但没有 BACKUP_OFFSITE_ENDPOINT_URL，'
                '异地镜像不会执行；要么补全端点，要么把这些变量全部清空'
            )
        return None
    bucket = _env('BACKUP_OFFSITE_BUCKET')
    if not bucket:
        raise ValueError('缺少 BACKUP_OFFSITE_BUCKET，异地镜像没有目标 bucket。')
    access_key = _env('BACKUP_OFFSITE_ACCESS_KEY_ID')
    secret_key = _env('BACKUP_OFFSITE_SECRET_ACCESS_KEY')
    if not access_key or not secret_key:
        raise ValueError(
            '缺少 BACKUP_OFFSITE_ACCESS_KEY_ID / BACKUP_OFFSITE_SECRET_ACCESS_KEY，'
            '异地镜像无法通过鉴权。'
        )
    retention_raw = _env('BACKUP_OFFSITE_RETENTION_DAYS') or _env('BACKUP_RETENTION_DAYS', '14')
    try:
        retention_days = int(retention_raw)
    except ValueError as exc:
        raise ValueError('BACKUP_OFFSITE_RETENTION_DAYS 必须是整数天数。') from exc
    return {
        'endpoint_url': endpoint,
        'bucket': bucket,
        'access_key_id': access_key,
        'secret_access_key': secret_key,
        'region': _env('BACKUP_OFFSITE_REGION', 'us-east-1') or 'us-east-1',
        'prefix': object_listing_prefix(
            _env('BACKUP_OFFSITE_PREFIX', 'campus-glimmer-backups')),
        'retention_days': retention_days,
    }


def offsite_client(config):
    return boto3.session.Session().client(
        's3',
        endpoint_url=config['endpoint_url'],
        region_name=config['region'],
        aws_access_key_id=config['access_key_id'],
        aws_secret_access_key=config['secret_access_key'],
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': 'path'},
        ),
    )


def _offsite_files(root, manifest):
    """归档里必须原样出现在异地的文件：库、清单和每一个对象镜像。"""
    root = Path(root).resolve()
    files = ['database.dump', 'manifest.json']
    files.extend(entry['file'] for entry in manifest['object_storage']['objects'])
    return [(relative, _safe_backup_path(root, relative)) for relative in files]


def _remote_keys(client, bucket, prefix):
    keys = set()
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            keys.add(obj['Key'])
    return keys


def mirror_backup(backup_dir, *, config, client=None):
    """把一份已校验的本地归档原样推到异地 bucket，并逐字节读回比对。

    「上传返回 200」不等于「备份成功」：被截断的上传在列表里照样是一个完整
    key。所以每个对象都重新下载，按 manifest 里的 SHA-256 比对字节；最后再把
    远端的 key 集合和预期集合对一遍，缺一个都算这次备份失败。
    """
    manifest = verify_backup(backup_dir)
    root = Path(backup_dir).resolve()
    client = client or offsite_client(config)
    name = root.name
    scope = f"{config['prefix']}{name}/"

    uploaded = []
    for relative, path in _offsite_files(root, manifest):
        key = f'{scope}{relative}'
        client.upload_file(str(path), config['bucket'], key)
        body = client.get_object(Bucket=config['bucket'], Key=key)['Body'].read()
        if hashlib.sha256(body).hexdigest() != sha256_file(path):
            raise ValueError(f'异地副本校验失败：{key}')
        uploaded.append(key)

    expected = set(uploaded)
    listed = _remote_keys(client, config['bucket'], scope)
    if listed != expected:
        missing = sorted(expected - listed)
        extra = sorted(listed - expected)
        raise ValueError(
            '异地副本的 key 集合与本地归档不一致：'
            f'缺少 {missing or "无"}，多出 {extra or "无"}'
        )
    return {
        'name': name,
        'bucket': config['bucket'],
        'prefix': scope,
        'keys': len(expected),
        'bytes': sum(path.stat().st_size for _, path in _offsite_files(root, manifest)),
    }


def prune_offsite(config, retention_days, *, now=None, client=None):
    """删除异地 bucket 里过期的 ``backup-*`` 归档。

    创建时间只从每个归档自己的 manifest.json 读，读不到就跳过：宁可多留一天，
    也不能凭 key 名猜一个时间去删别人的数据。
    """
    if retention_days < 1:
        return []
    client = client or offsite_client(config)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    prefix = config['prefix']
    groups = {}
    for key in sorted(_remote_keys(client, config['bucket'], prefix)):
        rest = key[len(prefix):]
        name, separator, relative = rest.partition('/')
        if not separator or not relative or not name.startswith(BACKUP_PREFIX):
            continue
        groups.setdefault(name, []).append(key)

    removed = []
    for name in sorted(groups):
        keys = groups[name]
        manifest_key = f'{prefix}{name}/manifest.json'
        if manifest_key not in keys:
            continue
        try:
            body = client.get_object(
                Bucket=config['bucket'], Key=manifest_key)['Body'].read()
            created = datetime.fromisoformat(json.loads(body)['created_at'])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (ClientError, OSError, ValueError, TypeError):
            # 一个读不动的 manifest 不能拦住别的过期归档被清理，
            # 但也不能反过来让"读不到"变成"删掉"的理由。
            continue
        if created >= cutoff:
            continue
        for key in keys:
            client.delete_object(Bucket=config['bucket'], Key=key)
        removed.append(name)
    return removed


def object_listing_prefix(prefix):
    """Keep a storage location from matching sibling keys with the same stem."""
    normalized = (prefix or '').strip('/')
    return f'{normalized}/' if normalized else ''


def _download_objects(client, bucket, prefix, objects_dir):
    entries = []
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(
        Bucket=bucket,
        Prefix=object_listing_prefix(prefix),
    ):
        for obj in page.get('Contents', []):
            key = obj['Key']
            safe_name = f'{len(entries):08d}-{Path(key).name or "object"}'
            destination = Path(objects_dir) / safe_name
            digest = hashlib.sha256()
            with client.get_object(Bucket=bucket, Key=key)['Body'] as body:
                with destination.open('wb') as handle:
                    for chunk in iter(lambda: body.read(1024 * 1024), b''):
                        digest.update(chunk)
                        handle.write(chunk)
            entries.append({
                'key': key,
                'file': f'objects/{safe_name}',
                'size': destination.stat().st_size,
                'sha256': digest.hexdigest(),
                'etag': obj.get('ETag', '').strip('"'),
                'last_modified': obj['LastModified'].isoformat()
                if obj.get('LastModified') else None,
            })
    return entries


def _safe_backup_path(root, relative):
    """Resolve a manifest path without allowing it to escape the backup root."""
    root = Path(root).resolve()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError(f'备份清单包含非法路径：{relative}')
    return candidate


def verify_backup(backup_dir):
    """Validate every checksum and size recorded in a backup manifest."""
    root = Path(backup_dir).resolve()
    manifest_path = root / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f'无法读取备份清单：{type(exc).__name__}') from exc

    if manifest.get('format_version') != MANIFEST_FORMAT:
        raise ValueError('备份清单版本不受支持。')

    problems = []
    database = manifest.get('database') or {}
    try:
        database_path = _safe_backup_path(root, database.get('file', ''))
        if database_path.stat().st_size != int(database.get('size', -1)):
            problems.append('database.dump 大小不匹配')
        if sha256_file(database_path) != database.get('sha256'):
            problems.append('database.dump SHA-256 不匹配')
    except (OSError, TypeError, ValueError):
        problems.append('database.dump 缺失或路径非法')

    object_storage = manifest.get('object_storage') or {}
    objects = object_storage.get('objects') or []
    if object_storage.get('count') != len(objects):
        problems.append('对象数量与清单不一致')
    for entry in objects:
        try:
            object_path = _safe_backup_path(root, entry.get('file', ''))
            if object_path.stat().st_size != int(entry.get('size', -1)):
                problems.append(f"对象大小不匹配：{entry.get('key', '')}")
            elif sha256_file(object_path) != entry.get('sha256'):
                problems.append(f"对象 SHA-256 不匹配：{entry.get('key', '')}")
        except (OSError, TypeError, ValueError):
            problems.append(f"对象缺失或路径非法：{entry.get('key', '')}")

    if problems:
        raise ValueError('；'.join(problems))
    return manifest


def _write_manifest(staging_dir, *, database, bucket, prefix, objects):
    dump_path = Path(staging_dir) / 'database.dump'
    manifest = {
        'format_version': MANIFEST_FORMAT,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'database': {
            'host': database['host'],
            'port': database['port'],
            'name': database['name'],
            'file': 'database.dump',
            'size': dump_path.stat().st_size,
            'sha256': sha256_file(dump_path),
        },
        'object_storage': {
            'bucket': bucket,
            'prefix': prefix,
            'count': len(objects),
            'objects': objects,
        },
    }
    (Path(staging_dir) / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return manifest


def create_backup(output_dir, *, database_url, bucket, prefix='', client=None):
    """Create one backup and return its final directory."""
    if not database_url:
        raise ValueError('缺少 DJANGO_DATABASE_URL。')
    if not bucket:
        raise ValueError('缺少 DJANGO_S3_BUCKET。')

    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    staging = root / f'.backup-{stamp}-{uuid.uuid4().hex[:8]}'
    staging.mkdir()
    objects_dir = staging / 'objects'
    objects_dir.mkdir()
    try:
        database = _dump_database(database_url, staging / 'database.dump')
        client = client or _s3_client()
        objects = _download_objects(client, bucket, prefix, objects_dir)
        _write_manifest(
            staging,
            database=database,
            bucket=bucket,
            prefix=prefix,
            objects=objects,
        )
        verify_backup(staging)
        final = root / f'{BACKUP_PREFIX}{stamp}'
        if final.exists():
            final = root / f'{BACKUP_PREFIX}{stamp}-{uuid.uuid4().hex[:8]}'
        staging.rename(final)
        return final
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def restore_objects(backup_dir, *, bucket, client=None):
    """Upload the verified object mirror back to its original S3 keys.

    This is intentionally additive: it never deletes objects that are absent
    from the backup, because an old manifest must not erase newer uploads.
    """
    manifest = verify_backup(backup_dir)
    root = Path(backup_dir).resolve()
    client = client or _s3_client()
    entries = manifest['object_storage']['objects']
    for entry in entries:
        object_path = _safe_backup_path(root, entry['file'])
        client.upload_file(str(object_path), bucket, entry['key'])
    return len(entries)


def prune_backups(root, retention_days, *, now=None):
    """Delete only expired ``backup-*`` directories below ``root``."""
    root = Path(root).resolve()
    if retention_days < 1 or not root.exists():
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    removed = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or not candidate.name.startswith(BACKUP_PREFIX):
            continue
        resolved = candidate.resolve()
        if root not in resolved.parents:
            continue
        manifest_path = resolved / 'manifest.json'
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            created = datetime.fromisoformat(manifest['created_at'])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            # One unreadable manifest must not stop the other expiries.
            continue
        if created < cutoff:
            shutil.rmtree(resolved)
            removed.append(resolved)
    return removed


def run_once():
    output_dir = _env('BACKUP_DIR', '/backups')
    retention_days = int(_env('BACKUP_RETENTION_DAYS', '14'))
    final = create_backup(
        output_dir,
        database_url=_env('DJANGO_DATABASE_URL'),
        bucket=_env('DJANGO_S3_BUCKET'),
        prefix=_env('DJANGO_S3_LOCATION'),
    )
    removed = prune_backups(output_dir, retention_days)
    print(f'backup created: {final}')
    if removed:
        print(f'expired backups removed: {len(removed)}')

    offsite = offsite_config()
    if offsite is not None:
        mirrored = mirror_backup(final, config=offsite)
        print(
            f"backup mirrored: {mirrored['keys']} objects -> "
            f"s3://{mirrored['bucket']}/{mirrored['prefix']}"
        )
        stale = prune_offsite(offsite, offsite['retention_days'])
        if stale:
            print(f'expired offsite backups removed: {len(stale)}')
    return final


def main():
    parser = argparse.ArgumentParser(description='Back up PostgreSQL and object storage.')
    parser.add_argument('--once', action='store_true', help='Run one backup and exit.')
    parser.add_argument('--verify', metavar='PATH', help='Verify one backup directory and exit.')
    parser.add_argument('--restore-objects', metavar='PATH', help='Restore object storage from one backup and exit.')
    parser.add_argument('--restore-bucket', help='Destination bucket for --restore-objects.')
    parser.add_argument('--mirror', metavar='PATH', help='Mirror one backup directory to the offsite bucket and exit.')
    parser.add_argument('--prune-offsite', action='store_true', help='Prune expired archives in the offsite bucket and exit.')
    parser.add_argument(
        '--interval-seconds',
        type=int,
        default=int(_env('BACKUP_INTERVAL_SECONDS', '86400')),
        help='Seconds between backups in daemon mode.',
    )
    args = parser.parse_args()
    if args.verify:
        verify_backup(args.verify)
        print(f'backup verified: {Path(args.verify).resolve()}')
        return
    if args.restore_objects:
        manifest = verify_backup(args.restore_objects)
        bucket = args.restore_bucket or _env('DJANGO_S3_BUCKET') or manifest['object_storage']['bucket']
        count = restore_objects(args.restore_objects, bucket=bucket)
        print(f'objects restored: {count} -> {bucket}')
        return
    if args.mirror:
        config = offsite_config()
        if config is None:
            raise SystemExit('未配置 BACKUP_OFFSITE_ENDPOINT_URL，无法执行异地镜像。')
        mirrored = mirror_backup(args.mirror, config=config)
        print(
            f"backup mirrored: {mirrored['keys']} objects -> "
            f"s3://{mirrored['bucket']}/{mirrored['prefix']}"
        )
        return
    if args.prune_offsite:
        config = offsite_config()
        if config is None:
            raise SystemExit('未配置 BACKUP_OFFSITE_ENDPOINT_URL，无法清理异地备份。')
        stale = prune_offsite(config, config['retention_days'])
        print(f'expired offsite backups removed: {len(stale)}')
        return
    if args.once:
        run_once()
        return
    interval = max(args.interval_seconds, 60)
    while True:
        try:
            run_once()
        except Exception as exc:  # keep the sidecar alive and visible in logs
            print(f'backup failed: {type(exc).__name__}: {exc}', flush=True)
        time.sleep(interval)


if __name__ == '__main__':
    main()
