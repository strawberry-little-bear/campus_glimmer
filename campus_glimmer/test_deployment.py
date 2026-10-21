import hashlib
import importlib.util
import json
import re
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


class VendoredStaticAssetTests(SimpleTestCase):
    """首屏资源必须完全本地化。

    这些断言盯的是两类只在生产环境才暴露的故障：

    1. 回归到 CDN：页面在本地开发时看起来正常，但国内网络访问不到
       unpkg / Google Fonts 时会退化成无样式 HTML。
    2. collectstatic 直接失败：Django 的 Manifest 静态文件存储会解析 CSS 里的
       url() 与 sourceMappingURL，引用不存在的文件会让整个构建崩掉，
       而这件事只在 DEBUG=False 的部署流程里才会发生。
    """

    VENDOR_DIR = PROJECT_ROOT / 'static' / 'vendor'
    VENDORED_FILES = (
        'bootstrap.min.css',
        'bootstrap.bundle.min.js',
        'bootstrap-icons.min.css',
        'fonts/bootstrap-icons.woff2',
        'fonts/bootstrap-icons.woff',
    )
    THIRD_PARTY_HOSTS = (
        'unpkg.com',
        'cdn.jsdelivr.net',
        'cdnjs.cloudflare.com',
        'fonts.googleapis.com',
        'fonts.gstatic.com',
    )

    def test_base_template_loads_every_asset_from_our_own_domain(self):
        base = (PROJECT_ROOT / 'templates' / 'base.html').read_text(encoding='utf-8')

        for host in self.THIRD_PARTY_HOSTS:
            with self.subTest(host=host):
                self.assertNotIn(host, base)

        # 连 preconnect 都不该有：没有任何跨域请求需要预热
        for line in base.splitlines():
            for match in re.finditer(r'(?:href|src)="(https?://[^"]+)"', line):
                with self.subTest(url=match.group(1)):
                    self.fail('base.html 仍在引用外部资源：%s' % match.group(1))

    def test_base_template_points_at_the_vendored_copies(self):
        base = (PROJECT_ROOT / 'templates' / 'base.html').read_text(encoding='utf-8')
        for asset in ('bootstrap.min.css', 'bootstrap-icons.min.css', 'bootstrap.bundle.min.js'):
            with self.subTest(asset=asset):
                self.assertIn("{%% static 'vendor/%s' %%}" % asset, base)

    def test_vendored_dependencies_are_checked_in(self):
        for name in self.VENDORED_FILES:
            path = self.VENDOR_DIR / name
            with self.subTest(file=name):
                self.assertTrue(path.is_file(), '%s 没有入库' % name)
                # 空文件或被截断的下载都会让页面静默缺样式
                self.assertGreater(path.stat().st_size, 1024, '%s 体积可疑地小' % name)

    def test_vendored_css_only_references_files_that_exist(self):
        for css in sorted(self.VENDOR_DIR.glob('*.css')):
            text = css.read_text(encoding='utf-8')
            with self.subTest(css=css.name):
                # sourceMappingURL 会让 ManifestStaticFilesStorage 去找 .map 文件，
                # 找不到就整条 collectstatic 失败。
                self.assertNotIn('sourceMappingURL', text)

                for match in re.finditer(r'url\(([^)]+)\)', text):
                    reference = match.group(1).strip('\'"')
                    if reference.startswith('data:'):
                        continue
                    # 去掉 CDN 自带的缓存查询串，只保留路径部分
                    reference = reference.split('?', 1)[0]
                    target = (css.parent / reference).resolve()
                    self.assertTrue(
                        target.is_file(),
                        '%s 引用了不存在的 %s' % (css.name, reference),
                    )

    def test_body_font_stack_does_not_depend_on_a_remote_font(self):
        css = (PROJECT_ROOT / 'static' / 'css' / 'style.css').read_text(encoding='utf-8')
        self.assertNotIn('@import', css)
        for host in self.THIRD_PARTY_HOSTS:
            self.assertNotIn(host, css)
        # 中文优先的系统栈：各平台都有完整字形覆盖，零请求、零 FOUT
        for family in ('PingFang SC', 'Microsoft YaHei', 'system-ui'):
            self.assertIn(family, css)


class TemplateStylesheetContractTests(SimpleTestCase):
    """模板里写上的每个自定义类名，都必须能在样式表里找到对应规则。

    运营看板是逐个分区长出来的，随之长出了一批「只有一个面板在用」的类名：
    类名写进了模板，样式却始终没有补上。这类类名不产生任何效果，
    却让人以为面板已经被单独调过；清理时只能靠肉眼逐个比对，成本很高。

    同一套检查还排除过另外两类只在浏览器里才暴露的问题：

    1. 图标类名写错：Bootstrap Icons 没有对应字形时页面不会报错，
       只会安静地渲染出一个空白占位，首页统计条上尤其显眼。
    2. 状态修饰类拼写漂移：账户中心的统计卡片靠 is-warm / is-blue / is-gold
       区分色相，拼错一个就静默掉回默认主色。
    """

    TEMPLATES_DIR = PROJECT_ROOT / 'templates'
    VENDOR_DIR = PROJECT_ROOT / 'static' / 'vendor'
    STYLESHEET = PROJECT_ROOT / 'static' / 'css' / 'style.css'

    # 这些类名不承担样式，是行为钩子：JS 或测试按它们定位元素，
    # 删掉不会改变观感，却会直接搞坏功能。
    BEHAVIOUR_HOOKS = frozenset({
        'bulk-item-checkbox',        # my_items.html 内联 onclick 的批量选择
        'confirm-delete',            # static/js/main.js 删除确认
        'message-select',            # inbox.html 内联脚本的消息多选
        'notification-bulk-submit',  # notifications.html 内联脚本的批量已读
        'operations-panels',         # main.js 懒加载看板分区
    })
    # 测试按这些类名截取页面片段，属于对外契约
    TEXT_ANCHORS = frozenset({
        'chat-trade-correlation-panel',
        'search-exposure-cold-start-panel',
    })
    # 设计文档里按这个名字记载过记录，改名会同时让文档失效
    DOCUMENTED_NAMES = frozenset({
        'mutual-aid-feedback-panel',
        'notification-preference-usage-panel',
    })

    HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)
    DYNAMIC_CLASS = re.compile(r'\{%[^%]*%\}|\{\{[^}]*\}\}')
    CLASS_ATTR = re.compile(r'class="([^"]*)"')

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.vendored = set()
        for css in sorted(cls.VENDOR_DIR.glob('*.css')):
            cls.vendored.update(
                re.findall(r'\.([a-zA-Z][\w-]*)', css.read_text(encoding='utf-8'))
            )
        cls.ours = set(
            re.findall(r'\.([a-zA-Z][\w-]*)', cls.STYLESHEET.read_text(encoding='utf-8'))
        )
        cls.allowed = cls.BEHAVIOUR_HOOKS | cls.TEXT_ANCHORS | cls.DOCUMENTED_NAMES

    def test_vendored_stylesheets_are_readable(self):
        # 前置条件：拉不到 vendor 类名表时，下面的断言会把所有
        # Bootstrap 类名当成未定义，报错信息就会失真。
        self.assertIn('btn-primary', self.vendored)
        self.assertIn('row', self.vendored)
        self.assertIn('bi-search', self.vendored)
        self.assertIn('account-stat-icon', self.ours)

    def test_every_custom_class_token_has_a_rule(self):
        missing = []
        for template in sorted(self.TEMPLATES_DIR.rglob('*.html')):
            text = self.HTML_COMMENT.sub(' ', template.read_text(encoding='utf-8'))
            for attr in self.CLASS_ATTR.findall(text):
                # class 里带模板语法时无法静态判断最终类名，整条跳过
                if self.DYNAMIC_CLASS.search(attr):
                    continue
                for token in attr.split():
                    if token in self.vendored or token in self.ours:
                        continue
                    if token in self.allowed:
                        continue
                    missing.append('%s -> %s' % (
                        template.relative_to(self.TEMPLATES_DIR).as_posix(), token))

        self.assertEqual(
            missing, [],
            '以下类名在模板里用了，但 style.css 没有对应规则（不会有任何效果）：\n'
            + '\n'.join('  ' + item for item in sorted(missing)),
        )

    def test_icon_classes_exist_in_the_vendored_icon_font(self):
        icons = (self.VENDOR_DIR / 'bootstrap-icons.min.css').read_text(encoding='utf-8')
        defined = set(re.findall(r'\.(bi-[\w-]+)', icons))
        broken = []
        for template in sorted(self.TEMPLATES_DIR.rglob('*.html')):
            text = self.HTML_COMMENT.sub(' ', template.read_text(encoding='utf-8'))
            for token in re.findall(r'\bbi-([\w-]+)', text):
                name = 'bi-' + token
                if name in defined:
                    continue
                broken.append('%s -> %s' % (
                    template.relative_to(self.TEMPLATES_DIR).as_posix(), name))

        self.assertEqual(
            broken, [],
            '以下图标类名在已引入的 Bootstrap Icons 里没有字形，'
            '页面会安静地渲染成空白：\n'
            + '\n'.join('  ' + item for item in sorted(broken)),
        )

class PanelFootnoteColourTests(SimpleTestCase):
    """运营面板脚注的色相由 --panel-footnote 提供，接管它的规则必须压过段落配色。

    每个面板都有一条「.xxx-panel p { color: ... }」，而模板里的脚注恰好就是
    <p class="rewrite-candidate-footnote">。一旦接管颜色的规则只有 (0,1,0)，
    段落配色就会以 (0,1,1) 胜出，脚注安静地变成和正文同一个颜色：
    页面不报错，也没有测试会红，只有对着截图才看得出来。

    这个坑真实踩过：曾经把九条「.xxx-panel .rewrite-candidate-footnote」收成一条
    带 var() 的通用规则，特异性从 (0,2,0) 掉到 (0,1,0)，九个面板的脚注同时失色。
    """

    STYLESHEET = PROJECT_ROOT / 'static' / 'css' / 'style.css'

    # 注释里也会出现「.xxx-panel」这类示例选择器，不剥掉就会把
    # 特异性算高，把真正偏弱的规则放过。
    CSS_COMMENT = re.compile(r'/\*.*?\*/', re.S)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.css = cls.CSS_COMMENT.sub(
            ' ', cls.STYLESHEET.read_text(encoding='utf-8'))

    @staticmethod
    def _specificity(selector):
        ids = len(re.findall(r'#[\w-]+', selector))
        classes = (len(re.findall(r'\.[\w-]+', selector))
                   + len(re.findall(r'\[[^\]]*\]', selector)))
        types = len(re.findall(r'(?:^|[\s>+~])([a-zA-Z][\w-]*)', selector))
        return (ids, classes, types)

    def _footnote_rules(self):
        for match in re.finditer(r'([^{}]+)\{([^{}]*)\}', self.css):
            selector = match.group(1).strip()
            if '.rewrite-candidate-footnote' not in selector:
                continue
            yield selector, match.group(2)

    def test_panels_declare_a_valid_footnote_colour(self):
        declared = re.findall(
            r'\.([\w-]+)\s*\{[^}]*--panel-footnote:\s*([^;]+);', self.css)
        self.assertTrue(declared, 'style.css 里没有面板声明 --panel-footnote')

        broken = []
        for panel, colour in declared:
            colour = colour.strip()
            if not re.fullmatch(r'#[0-9a-fA-F]{6}', colour):
                broken.append('%s -> %r' % (panel, colour))
        self.assertEqual(
            broken, [],
            '以下面板的 --panel-footnote 不是六位十六进制色值：\n'
            + '\n'.join('  ' + item for item in sorted(broken)),
        )

    def test_footnote_rule_outranks_the_panel_paragraph_colour(self):
        winners = [
            (selector, body) for selector, body in self._footnote_rules()
            if 'var(--panel-footnote' in body
        ]
        self.assertTrue(
            winners,
            '没有规则把 var(--panel-footnote) 应用到 .rewrite-candidate-footnote，'
            '面板声明的脚注色相不会生效',
        )

        weak = []
        for selector, _ in winners:
            specificity = self._specificity(selector)
            # .xxx-panel p 的特异性是 (0,1,1)，必须严格高于它
            if specificity <= (0, 1, 1):
                weak.append('%s -> %s' % (selector, specificity))
        self.assertEqual(
            weak, [],
            '以下规则接管了脚注颜色，但特异性不高于「.xxx-panel p」(0,1,1)，'
            '脚注会被面板的段落配色盖掉：\n'
            + '\n'.join('  ' + item for item in sorted(weak)),
        )

    def test_default_footnote_colour_has_a_fallback(self):
        bases = [
            body for _, body in self._footnote_rules()
            if re.search(r'color:\s*#[0-9a-fA-F]{6}', body)
        ]
        self.assertTrue(
            bases,
            '.rewrite-candidate-footnote 缺少默认颜色，面板外的脚注会掉成继承色',
        )

    def test_footnote_fallback_matches_the_muted_token(self):
        """面板脚注的回落色必须跟 --muted 一致。

        回落值写成字面色是为了让上一条测试能检查“有确切默认值”，
        但它同时也是一份可以追溯的副本：改了 --muted 却忘记同步这里，
        面板外的脚注会安静停留在旧色值上。
        """
        match = re.search(r'--muted:\s*(#[0-9a-fA-F]{6})', self.css)
        self.assertIsNotNone(match, 'style.css 里找不到 --muted 定义')

        fallbacks = [
            value for _, body in self._footnote_rules()
            for value in re.findall(
                r'var\(--panel-footnote,\s*(#[0-9a-fA-F]{6})\)', body)
        ]
        self.assertTrue(
            fallbacks,
            '没有规则给 var(--panel-footnote) 提供字面回落色')
        stale = [value for value in fallbacks if value.lower() != match.group(1).lower()]
        self.assertEqual(
            stale, [],
            '以下回落色与 --muted 不一致，面板外的脚注会用到过期色值：\n'
            + '\n'.join('  ' + item for item in stale))
