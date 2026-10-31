import hashlib
import importlib.util
import io
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError
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
    STYLESHEETS = (
        PROJECT_ROOT / 'static' / 'css' / 'style.css',
        PROJECT_ROOT / 'static' / 'css' / 'error-pages.css',
    )

    # 这些类名不承担样式，是行为钩子：JS 或测试按它们定位元素，
    # 删掉不会改变观感，却会直接搞坏功能。
    BEHAVIOUR_HOOKS = frozenset({
        'bulk-item-checkbox',        # my_items.html 内联 onclick 的批量选择
        'confirm-delete',            # static/js/main.js 删除确认
        'message-select',            # inbox.html 内联脚本的消息多选
        'notification-bulk-submit',  # notifications.html 内联脚本的批量已读
        'operations-panels',         # main.js 懒加载看板分区
        'search-insights-panels',    # main.js 懒加载搜索洞察分区
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
        cls.ours = set()
        for stylesheet in cls.STYLESHEETS:
            cls.ours.update(
                re.findall(r'\.([a-zA-Z][\w-]*)', stylesheet.read_text(encoding='utf-8'))
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
            '以下类名在模板里用了，但已加载的样式表没有对应规则（不会有任何效果）：\n'
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

class InlineStyleColourTests(SimpleTestCase):
    """模板的 style 属性里不允许出现写死的颜色值。

    颜色全部走 :root token 是这条样式系统的前提：token 改一处，全站跟着走，
    对比度也能一次性重算。写在 style 属性里的十六进制色值两边都不占——
    既不会被 token 扫描覆盖，也不会被静态对比度扫描覆盖，只会安静地漂移。

    这个坑真实踩过：运营看板的一条建议文案在模板里写了 color:#6a7d73，
    盖掉了已有的 token 规则，落在 #fbfdfb 上只有 4.29:1。浏览器审计跑了
    三轮才发现，因为静态扫描只读 style.css，读不到模板里的行内样式。
    """

    TEMPLATES_DIR = PROJECT_ROOT / 'templates'

    HTML_COMMENT = re.compile(r'<!--.*?-->', re.S)
    TEMPLATE_SYNTAX = re.compile(r'\{%[^%]*%\}|\{\{[^}]*\}\}')
    STYLE_ATTR = re.compile(r'style="([^"]*)"')
    # 只盯承担配色的属性；width / height / min-width 这类几何值不在此列，
    # 它们常常必须由视图算出来后注入（例如按比例增长的条形图）。
    COLOUR_PROPERTIES = frozenset({
        'color', 'background', 'background-color',
        'border-color', 'border-top-color', 'border-right-color',
        'border-bottom-color', 'border-left-color',
        'outline-color', 'fill', 'stroke', 'box-shadow',
    })

    def test_inline_styles_do_not_hardcode_colours(self):
        offenders = []
        for template in sorted(self.TEMPLATES_DIR.rglob('*.html')):
            text = self.HTML_COMMENT.sub(' ', template.read_text(encoding='utf-8'))
            for body in self.STYLE_ATTR.findall(text):
                for declaration in body.split(';'):
                    name, _, value = declaration.partition(':')
                    name = name.strip().lower()
                    value = value.strip()
                    if name not in self.COLOUR_PROPERTIES:
                        continue
                    # 带模板语法的值无法静态判断，整条跳过
                    if self.TEMPLATE_SYNTAX.search(value):
                        continue
                    if value.startswith('var('):
                        continue
                    offenders.append('%s -> %s:%s' % (
                        template.relative_to(self.TEMPLATES_DIR).as_posix(),
                        name, value))

        self.assertEqual(
            offenders, [],
            '以下 style 属性写死了颜色值，请改用 :root token：\n'
            + '\n'.join('  ' + item for item in sorted(offenders)),
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


class StylesheetSyntaxTests(SimpleTestCase):
    """样式表自身必须语法完好：一个字符的错位会让它之后的规则全部静默失效。

    真实事故：`static/css/style.css` 在命中率分层那段少了一个右花括号，
    浏览器从出错位置开始把后面的内容整体丢弃——新写的认证页、导航菜单和
    搜索条全都没有生效。页面不报错、控制台不警告，也没有任何视觉线索，
    只能靠逐张截图比对才发现。

    现有的检查都拦不住这一类问题：Django 的 `check` 和 `collectstatic`
    只确认文件存在、能搬走；`TemplateStylesheetContractTests` 比对类名时
    是用正则从原文里刮出来的，语法坏掉反而更容易「刮得到」。所以这条线
    只能靠直接解析样式表来守。

    这里只覆盖自己维护的 `static/css/`。vendor 下的第三方样式表是上游产物，
    体积大且不由本项目修改，语法问题应该回到上游而不是在这里报警。

    检查五类问题：

    1. 未闭合的注释：`/*` 没有配对的 `*/`，其后所有规则都变成注释；
    2. 未闭合的字符串：`content: "..."` 少一个引号，会把后面的规则吞进字符串；
    3. 多余的右花括号：顶层出现没有对应左花括号的 `}`；
    4. 左花括号没有配对的右花括号：报错时给出左花括号所在行，因为那才是
       需要补括号的位置；
    5. 游离的声明块：`{` 前面没有选择器，说明前一条规则被提前闭合了。
    """

    CSS_DIR = PROJECT_ROOT / 'static' / 'css'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.stylesheets = sorted(cls.CSS_DIR.rglob('*.css'))
        cls.assertTrue(cls.stylesheets, 'static/css/ 下没有找到样式表，检查路径是否搬走了')

    @staticmethod
    def _problems(text):
        """返回 [(问题类型, 行号)]，以及未闭合左花括号的行号栈。"""
        problems = []
        stack = []
        block_start = 0
        line = 1
        i, size = 0, len(text)
        while i < size:
            char = text[i]
            if char == '\n':
                line += 1
                i += 1
            elif char == '/' and text.startswith('/*', i):
                close = text.find('*/', i + 2)
                if close < 0:
                    problems.append(('未闭合的注释', line))
                    return problems, stack
                line += text.count('\n', i, close + 2)
                i = close + 2
            elif char in ('"', "'"):
                # CSS 字符串里可以合法出现花括号和注释起始符，
                # 先整段跳过，否则 url("data:...") 会把计数搅乱。
                j = i + 1
                while j < size and text[j] != char:
                    if text[j] == '\\':
                        j += 2
                        continue
                    if text[j] == '\n':
                        line += 1
                    j += 1
                if j >= size:
                    problems.append(('未闭合的字符串', line))
                    return problems, stack
                line += text.count('\n', i, j + 1)
                i = j + 1
            elif char == '{':
                if not text[block_start:i].strip():
                    problems.append(('游离的声明块，左花括号前没有选择器', line))
                stack.append(line)
                i += 1
            elif char == '}':
                if not stack:
                    problems.append(('多余的右花括号', line))
                else:
                    stack.pop()
                    block_start = i + 1
                i += 1
            else:
                i += 1
        return problems, stack

    def test_stylesheets_have_no_syntax_breaks(self):
        found = []
        for stylesheet in self.stylesheets:
            text = stylesheet.read_text(encoding='utf-8')
            problems, unclosed = self._problems(text)
            for kind, line in problems:
                found.append('%s:%d %s' % (stylesheet.name, line, kind))
            for line in unclosed:
                found.append('%s:%d 左花括号没有配对的右花括号' % (stylesheet.name, line))

        self.assertEqual(
            found, [],
            '样式表语法不完整，浏览器会从出错位置开始静默丢弃后续规则：\n'
            + '\n'.join('  ' + item for item in found))


def load_startup_tasks_module():
    path = PROJECT_ROOT / 'docker' / 'startup_tasks.py'
    spec = importlib.util.spec_from_file_location('campus_startup_tasks', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compose_services(path):
    """按 ``services:`` 下的顶层键切分 compose 段落。

    刻意不引入 PyYAML：它只是本机开发环境的间接依赖，并不在
    ``requirements.txt`` 里，CI 只装这一个文件。用真正的 YAML 解析器会让
    契约测试在干净的 CI runner 上直接 ImportError，把「配置漂移」伪装成
    「环境坏了」。这里只解析本项目 compose 实际使用的那一小类写法。
    """
    services = {}
    current = None
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if not line.startswith(' '):
            current = line.rstrip() == 'services:'
            continue
        if not current:
            continue
        indent = len(line) - len(line.lstrip(' '))
        stripped = line.strip()
        if indent == 2 and stripped.endswith(':') and not stripped.startswith('- '):
            current = stripped[:-1]
            services.setdefault(current, [])
            continue
        if current:
            services[current].append(line)
    return services


def block_value(lines, header):
    """取出某个 YAML 块内、直接子级的 ``key: value`` 映射。"""
    values = {}
    current = None
    inside = False
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(' '))
        if stripped == header:
            inside = True
            continue
        if inside and indent <= 4:
            break
        if not inside:
            continue
        if indent == 6 and stripped.endswith(':'):
            current = stripped[:-1]
            values[current] = None
        elif indent == 8 and ':' in stripped:
            key, _, value = stripped.partition(':')
            if current is not None and key.strip() == 'condition':
                values[current] = value.strip().strip('"')
    return values


def block_items(lines, header):
    """取出某个 YAML 块内的 ``- item`` 列表。"""
    items = []
    inside = False
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(' '))
        if stripped == header:
            inside = True
            continue
        if inside and indent <= 4:
            break
        if inside and stripped.startswith('- '):
            items.append(stripped[2:].strip().strip('"'))
    return items


class StartupTaskTests(SimpleTestCase):
    """迁移与 collectstatic 必须由一次性任务承担，且失败要中断发布。"""

    def setUp(self):
        self.module = load_startup_tasks_module()

    def test_default_runs_migrate_then_collectstatic_non_interactively(self):
        with patch.object(self.module, 'call_command') as call_command:
            executed = self.module.run_startup_tasks(stdout=io.StringIO())

        self.assertEqual(executed, ['migrate', 'collectstatic'])
        self.assertEqual(
            [call.args[0] for call in call_command.call_args_list],
            ['migrate', 'collectstatic'],
        )
        for call in call_command.call_args_list:
            with self.subTest(command=call.args[0]):
                self.assertIn('--noinput', call.args)

    def test_migrate_only_skips_collectstatic(self):
        with patch.object(self.module, 'call_command') as call_command:
            executed = self.module.run_startup_tasks(
                collect_static=False, stdout=io.StringIO())

        self.assertEqual(executed, ['migrate'])
        self.assertEqual(call_command.call_count, 1)

    def test_failure_stops_the_release_instead_of_serving_a_half_migrated_schema(self):
        with patch.object(
            self.module, 'call_command',
            side_effect=CommandError('迁移失败'),
        ) as call_command:
            with self.assertRaises(CommandError):
                self.module.run_startup_tasks(stdout=io.StringIO())

        self.assertEqual(call_command.call_count, 1)

    def test_collectstatic_failure_aborts_before_gunicorn_starts(self):
        def fail_on_collectstatic(name, *args, **kwargs):
            if name == 'collectstatic':
                raise CommandError('静态清单写入失败')

        with patch.object(
            self.module, 'call_command', side_effect=fail_on_collectstatic,
        ):
            with self.assertRaises(CommandError):
                self.module.run_startup_tasks(stdout=io.StringIO())

    def test_standalone_invocation_configures_django_first(self):
        # 生产 compose 里这个文件是「python docker/startup_tasks.py」直接跑的，
        # 没有 manage.py 帮忙调用 django.setup()，缺了这一步每条管理命令都会
        # 以 AppRegistryNotReady 收场。
        with patch.object(self.module.apps, 'ready', False), \
                patch.object(self.module.django, 'setup') as setup, \
                patch.object(self.module, 'call_command'):
            self.module.run_startup_tasks(stdout=io.StringIO())
        setup.assert_called_once_with()

    def test_does_not_reconfigure_an_already_configured_process(self):
        with patch.object(self.module.apps, 'ready', True), \
                patch.object(self.module.django, 'setup') as setup, \
                patch.object(self.module, 'call_command'):
            self.module.run_startup_tasks(stdout=io.StringIO())
        setup.assert_not_called()

    def test_main_maps_the_flag_onto_the_same_code_path(self):
        with patch.object(
            self.module, 'run_startup_tasks',
            return_value=['migrate'],
        ) as run_startup_tasks:
            self.assertEqual(self.module.main(['--migrate-only']), 0)
        run_startup_tasks.assert_called_once_with(collect_static=False)

        with patch.object(
            self.module, 'run_startup_tasks',
            return_value=['migrate', 'collectstatic'],
        ) as run_startup_tasks:
            self.assertEqual(self.module.main([]), 0)
        run_startup_tasks.assert_called_once_with(collect_static=True)


class ComposeProductionTopologyTests(SimpleTestCase):
    """多实例发布的启动任务边界，必须由配置本身保证。

    这里盯的是三个曾经只能靠肉眼发现的事故：

    1. ``web`` 还留在 entrypoint 里跑 migrate：扩容到多个副本后，每个 worker
       都会并发执行迁移和 collectstatic，后者会把 Manifest 静态文件的
       ``staticfiles.json`` 写裂，页面安静地丢掉全部样式。
    2. ``migrate`` 与 ``web`` 的环境变量各写一遍：改一处忘另一处，迁移用的
       连接串和服务用的连接串悄悄分叉。
    3. collectstatic 的产出只在 migrate 容器里：web 挂载不到共享卷，静态文件
       哈希和 manifest 对不上，DEBUG=False 下整站 500。
    """

    PROD_COMPOSE = PROJECT_ROOT / 'docker-compose.prod.yml'
    LOCAL_COMPOSE = PROJECT_ROOT / 'docker-compose.yml'
    ENTRYPOINT = PROJECT_ROOT / 'docker' / 'entrypoint.sh'

    def setUp(self):
        self.services = compose_services(self.PROD_COMPOSE)
        self.assertIn('migrate', self.services)
        self.assertIn('web', self.services)

    def _environment(self, name):
        values = {}
        inside = False
        for line in self.services[name]:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(' '))
            if stripped == 'environment:':
                inside = True
                continue
            if inside and indent <= 4:
                break
            if not inside:
                continue
            # <<: *app-env 是共享锚点，值在别处定义，不当成独立键
            if stripped.startswith('<<:'):
                continue
            key, _, value = stripped.partition(':')
            values[key.strip()] = value.strip().strip('"')
        return values

    def _scalar(self, name, key):
        """读取服务块内直接的 ``key: value`` 行。"""
        for line in self.services[name]:
            stripped = line.strip()
            if stripped.startswith(key + ':'):
                return stripped.split(':', 1)[1].strip().strip('"')
        return None

    def _sequence(self, name, key):
        """读取 ``key: [a, b]`` 行内序列或 ``- item`` 块序列。"""
        raw = self._scalar(name, key)
        if raw is None:
            return []
        if raw.startswith('['):
            body = raw.strip('[]')
            return [item.strip().strip('"') for item in body.split(',') if item.strip()]
        return block_items(self.services[name], key + ':')

    def _uses_shared_environment_anchor(self, name):
        inside = False
        for line in self.services[name]:
            stripped = line.strip()
            if stripped == 'environment:':
                inside = True
                continue
            if inside and len(line) - len(line.lstrip(' ')) <= 4:
                break
            if inside and stripped.startswith('<<:'):
                return stripped
        return None

    def _shared_environment_anchor(self):
        """解析 x-app-env 锚点块，返回它真正定义的键值。"""
        text = self.PROD_COMPOSE.read_text(encoding='utf-8')
        marker = 'x-app-env: &app-env'
        self.assertIn(marker, text, '共享环境变量锚点 x-app-env 被删掉了')
        values = {}
        inside = False
        for line in text.splitlines():
            if line.startswith(marker):
                inside = True
                continue
            if inside:
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                if not line.startswith(' '):
                    break
                key, _, value = line.strip().partition(':')
                values[key.strip()] = value.strip().strip('"')
        return values

    def test_web_never_runs_startup_tasks(self):
        self.assertEqual(
            self._environment('web').get('DJANGO_RUN_STARTUP_TASKS'), '0')
        self.assertEqual(self._sequence('web', 'entrypoint'), [])

    def test_migrate_is_a_one_shot_job(self):
        self.assertEqual(
            self._scalar('migrate', 'restart'), 'no')
        self.assertEqual(
            self._sequence('migrate', 'entrypoint'),
            ['python', 'docker/startup_tasks.py'],
        )

    def test_migrate_waits_for_every_dependency(self):
        depends = block_value(self.services['migrate'], 'depends_on:')
        self.assertEqual(depends.get('db'), 'service_healthy')
        self.assertEqual(depends.get('redis'), 'service_healthy')
        self.assertEqual(
            depends.get('minio_init'), 'service_completed_successfully')

    def test_web_starts_only_after_migrate_completed(self):
        depends = block_value(self.services['web'], 'depends_on:')
        self.assertEqual(
            depends.get('migrate'), 'service_completed_successfully')
        self.assertEqual(depends.get('db'), 'service_healthy')
        self.assertEqual(depends.get('redis'), 'service_healthy')
        self.assertEqual(
            depends.get('minio_init'), 'service_completed_successfully')

    def test_collectstatic_output_is_shared_and_read_only_for_web(self):
        self.assertEqual(
            self._sequence('migrate', 'volumes'),
            ['staticfiles:/app/staticfiles'],
        )
        self.assertEqual(
            self._sequence('web', 'volumes'),
            ['staticfiles:/app/staticfiles:ro'],
        )

    def test_shared_environment_anchor_cannot_drift(self):
        anchor = self._shared_environment_anchor()
        shared = ('DJANGO_DEBUG', 'DJANGO_REQUIRE_PRODUCTION_SERVICES',
                  'DJANGO_SESSION_ENGINE')

        for name in ('web', 'migrate'):
            with self.subTest(service=name):
                # 两个服务都必须从同一个锚点继承共享配置，而不是各写一遍
                self.assertEqual(
                    self._uses_shared_environment_anchor(name), '<<: *app-env')
                # 也不允许在服务块里偷偷覆盖共享键
                for key in shared:
                    self.assertNotIn(
                        key, self._environment(name),
                        '%s 覆盖了共享键 %s，锚点就失去意义了' % (name, key))

        for key in shared:
            with self.subTest(key=key):
                self.assertIn(key, anchor)
                self.assertTrue(anchor[key], '%s 在锚点里是空值' % key)
        self.assertEqual(anchor.get('DJANGO_DEBUG'), '0')
        self.assertEqual(anchor.get('DJANGO_REQUIRE_PRODUCTION_SERVICES'), '1')

    def test_only_the_startup_task_flag_differs_between_web_and_migrate(self):
        self.assertEqual(self._environment('web'), {'DJANGO_RUN_STARTUP_TASKS': '0'})
        self.assertEqual(
            self._environment('migrate'), {'DJANGO_RUN_STARTUP_TASKS': '1'})

    def test_production_compose_declares_the_shared_staticfiles_volume(self):
        text = self.PROD_COMPOSE.read_text(encoding='utf-8')
        volumes_block = text.splitlines()
        declared = False
        inside = False
        for line in volumes_block:
            if not line.startswith(' '):
                inside = line.rstrip() == 'volumes:'
                continue
            if inside and line.strip() == 'staticfiles:':
                declared = True
        self.assertTrue(declared, '顶层 volumes 缺少 staticfiles 命名卷')

    def test_local_compose_keeps_the_single_container_default(self):
        local = compose_services(self.LOCAL_COMPOSE)
        self.assertNotIn('migrate', local)
        environment = {}
        for line in local['web']:
            stripped = line.strip()
            if stripped.startswith('DJANGO_RUN_STARTUP_TASKS'):
                key, _, value = stripped.partition(':')
                environment[key.strip()] = value.strip().strip('"')
        self.assertEqual(environment.get('DJANGO_RUN_STARTUP_TASKS'), '1')

    def test_entrypoint_gates_startup_tasks_and_defaults_to_running_them(self):
        text = self.ENTRYPOINT.read_text(encoding='utf-8')
        self.assertIn('${DJANGO_RUN_STARTUP_TASKS:-1}', text)
        self.assertIn('python docker/startup_tasks.py', text)



class ContainerLivenessProbeTests(SimpleTestCase):
    """容器的 HEALTHCHECK 只能是存活探针。

    这条契约来自一次真实设计返工：``docker/healthcheck.py`` 最初只探
    ``/readyz/``，而 ``/readyz/`` 会连 PostgreSQL、Redis 和对象存储。数据库
    短暂抖动时，编排系统会把*每一个* web 副本一起重启，一次存储故障被放大成
    全站故障。设计文档（``docs/deployment-health-endpoints-design.md``）因此
    要求存活检查不碰依赖，但配置层面此前没有任何测试守着这句话。

    只测 ``healthcheck.py`` 的函数是不够的：真正决定容器行为的是 Dockerfile
    与 compose 里那条 CMD，把它改回 ``--readiness`` 不会有任何测试报警。
    """

    PROD_COMPOSE = PROJECT_ROOT / 'docker-compose.prod.yml'
    DOCKERFILE = PROJECT_ROOT / 'Dockerfile'

    def _dockerfile_healthcheck_command(self):
        """取 HEALTHCHECK 指令（含续行）里的 CMD 参数。

        续行以行尾反斜杠拼接，所以要一直读到 CMD 那一行为止，否则会把后面的
        EXPOSE / ENTRYPOINT 一起吞进来。
        """
        text = self.DOCKERFILE.read_text(encoding='utf-8')
        command = []
        collecting = False
        for line in text.splitlines():
            stripped = line.strip()
            if not collecting:
                collecting = stripped.startswith('HEALTHCHECK ')
                continue
            if stripped.upper().startswith('CMD '):
                command.append(stripped[4:])
                break
            command.append(stripped.rstrip('\\').strip())
        assert command, 'Dockerfile 的 HEALTHCHECK 里找不到 CMD'
        return self._inline_sequence(command[0])

    def _compose_healthcheck_command(self, service):
        services = compose_services(self.PROD_COMPOSE)
        lines = services[service]
        inside = False
        for line in lines:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(' '))
            if stripped == 'healthcheck:':
                inside = True
                continue
            if inside and indent <= 4:
                break
            if inside and stripped.startswith('test:'):
                return self._inline_sequence(stripped.split(':', 1)[1])
        self.fail('%s 服务没有声明 healthcheck.test' % service)

    @staticmethod
    def _inline_sequence(raw):
        body = raw.strip().strip('[]')
        return [item.strip().strip('"') for item in body.split(',') if item.strip()]

    def test_dockerfile_healthcheck_probes_liveness_only(self):
        command = self._dockerfile_healthcheck_command()

        self.assertIn('python /app/docker/healthcheck.py --liveness', ' '.join(command))
        self.assertNotIn('--readiness', ' '.join(command))

    def test_production_web_healthcheck_probes_liveness_only(self):
        command = self._compose_healthcheck_command('web')

        self.assertEqual(command[-1], '--liveness')
        self.assertNotIn('--readiness', command)

    def test_production_web_healthcheck_never_gates_on_dependencies(self):
        """没有任何服务会因 web 的 HEALTHCHECK 而等待，存活抖动才不会扩散。"""
        services = compose_services(self.PROD_COMPOSE)
        for name, lines in services.items():
            depends = block_value(lines, 'depends_on:')
            with self.subTest(service=name):
                self.assertNotEqual(
                    depends.get('web'), 'service_healthy',
                    '%s 在等 web 变成 healthy，存活探针的抖动会扩散成启动阻塞' % name)

    def test_local_compose_does_not_redefine_the_probe(self):
        services = compose_services(PROJECT_ROOT / 'docker-compose.yml')
        for name, lines in services.items():
            with self.subTest(service=name):
                self.assertFalse(
                    any(line.strip() == 'healthcheck:' for line in lines),
                    '本地 compose 不该覆盖探针语义，否则本地与线上行为分叉')


class ContainerLogRotationTests(SimpleTestCase):
    '''每个服务的容器日志都必须封顶。

    Docker 的 json-file 驱动默认 ``max-size: -1``，也就是不轮转。这个项目把
    gunicorn 的 access log 和 Django 日志都写在 stdout，单机长跑之后
    ``/var/lib/docker/containers`` 会把宿主磁盘吃光，症状是全站一起宕机，
    而应用自身的日志里一个字都不会留下。这属于配置事故，只能由配置本身兜住。
    '''

    PROD_COMPOSE = PROJECT_ROOT / 'docker-compose.prod.yml'

    def setUp(self):
        self.services = compose_services(self.PROD_COMPOSE)
        self.assertNotEqual(self.services, {})

    def _log_anchor(self):
        '''解析 x-log-defaults 锚点，返回嵌套后的真实键值。'''
        text = self.PROD_COMPOSE.read_text(encoding='utf-8')
        marker = 'x-log-defaults: &log-defaults'
        self.assertIn(marker, text, '共享日志配置锚点 x-log-defaults 被删掉了')
        anchor = {}
        section = None
        inside = False
        for line in text.splitlines():
            if line.startswith(marker):
                inside = True
                continue
            if not inside:
                continue
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            if not line.startswith(' '):
                break
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(' '))
            if indent == 2:
                key, _, value = stripped.partition(':')
                section = key
                anchor[section] = value.strip().strip('"') or None
            elif indent == 4 and section is not None:
                key, _, value = stripped.partition(':')
                if not isinstance(anchor[section], dict):
                    anchor[section] = {}
                anchor[section][key] = value.strip().strip('"')
        return anchor

    def _logging_children(self, name):
        children = []
        inside = False
        for line in self.services[name]:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip(' '))
            if stripped == 'logging:':
                inside = True
                continue
            if inside and indent <= 4:
                break
            if inside:
                children.append(stripped)
        return children

    def test_every_service_declares_log_rotation(self):
        missing = sorted(
            name for name in self.services if not self._logging_children(name))
        self.assertEqual(
            missing, [],
            '这些服务没有 logging 配置，日志会无限增长：%s' % ', '.join(missing))

    def test_every_service_inherits_the_shared_log_defaults(self):
        for name in sorted(self.services):
            with self.subTest(service=name):
                self.assertEqual(
                    self._logging_children(name), ['<<: *log-defaults'],
                    '%s 没有复用共享日志锚点，各写一份必然漂移' % name)

    def test_log_defaults_use_the_rotating_json_file_driver(self):
        self.assertEqual(self._log_anchor().get('driver'), 'json-file')

    def test_log_defaults_bound_the_size_on_disk(self):
        options = self._log_anchor().get('options') or {}
        self.assertIsInstance(options, dict, 'options 必须是一个映射')

        # max-size: -1 是 Docker 的默认值，等于没写
        self.assertRegex(
            options.get('max-size', ''), r'^\d+(\.\d+)?[bkmgt]?$',
            'max-size 必须是带单位的大小，-1 表示不轮转')
        self.assertLessEqual(
            float(options['max-size'].rstrip('bkmgt')), 64,
            '单个日志文件封顶不该超过 64m')

        self.assertRegex(options.get('max-file', ''), r'^\d+$')
        self.assertGreaterEqual(
            int(options['max-file']), 2, '至少要保留一份轮转前的日志')
        self.assertLessEqual(
            int(options['max-file']), 10, '保留份数太多照样能写满磁盘')
