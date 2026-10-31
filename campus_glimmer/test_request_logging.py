# campus_glimmer/test_request_logging.py
"""请求关联 ID 与日志配置的契约测试。

多副本部署下，一条用户投诉落到日志里只是某个副本的某一行。这里钉住三件事：
客户端传来的关联 ID 必须过检（否则等于允许日志注入）、日志行和响应头必须是
同一个 ID（否则关联不起来）、entrypoint 里的 gunicorn 参数必须真的能启动
容器（拼错一个选项只会在容器里以 unrecognized arguments 退出）。
"""

import logging
import re
import shlex
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from .middleware import (
    NO_REQUEST_ID,
    REQUEST_ID_HEADER,
    REQUEST_ID_META_KEY,
    RequestIdFilter,
    RequestIdMiddleware,
    current_request_id,
    new_request_id,
    sanitize_request_id,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = PROJECT_ROOT / 'docker' / 'entrypoint.sh'

# gunicorn 只认这些长选项。写错的拼法在本地毫无症状，只会在容器启动时以
# "unrecognized arguments" 退出——这正是本轮踩到的坑：argparse 的缩写匹配
# 不会把 --access-log-format 认成 --access-logformat。
GUNICORN_LONG_OPTIONS = {
    '--access-logfile',
    '--access-logformat',
    '--bind',
    '--capture-output',
    '--chdir',
    '--check-config',
    '--config',
    '--daemon',
    '--enable-stdio-inheritance',
    '--error-logfile',
    '--graceful-timeout',
    '--group',
    '--keep-alive',
    '--limit-request-line',
    '--log-level',
    '--max-requests',
    '--max-requests-jitter',
    '--preload',
    '--reload',
    '--reuse-port',
    '--spew',
    '--threads',
    '--timeout',
    '--umask',
    '--user',
    '--worker-class',
    '--worker-connections',
    '--worker-tmp-dir',
    '--workers',
}

# gunicorn access log 的原子字段，取自 gunicorn.glogging.atoms。
GUNICORN_ATOMS = set('hlutrsmUqHbfaTDMLp')


class SanitizeRequestIdTests(SimpleTestCase):
    def test_accepts_a_well_formed_client_supplied_id(self):
        self.assertEqual(sanitize_request_id('trace-7f3a.9_x'), 'trace-7f3a.9_x')

    def test_treats_missing_and_blank_as_absent(self):
        self.assertEqual(sanitize_request_id(''), '')
        self.assertEqual(sanitize_request_id(None), '')
        self.assertEqual(sanitize_request_id('   '), '')

    def test_rejects_ids_longer_than_the_limit(self):
        self.assertEqual(sanitize_request_id('a' * 65), '')
        self.assertEqual(sanitize_request_id('a' * 64), 'a' * 64)

    def test_rejects_header_injection_attempts(self):
        # 日志是按行消费的。放行换行符或分号，等于允许客户端往日志文件里
        # 伪造一整行记录，事后根本分不清哪行是真的。
        for payload in (
            'abc\n2026-01-01 00:00:00 INFO [x] fake entry',
            'a\r\nb',
            'a b',
            'a;rm -rf /',
            'id"quoted"',
            'id%0d%0a',
            '\u8bf7\u6c42\u6807\u8bc6',
        ):
            with self.subTest(payload=payload):
                self.assertEqual(sanitize_request_id(payload), '')

    def test_generated_ids_are_unique_and_within_the_limit(self):
        seen = {new_request_id() for _ in range(200)}
        self.assertEqual(len(seen), 200)
        for value in seen:
            self.assertRegex(value, r'^[0-9a-f]{16}$')


class RequestIdMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _run(self, header=None):
        seen = {}

        def get_response(request):
            seen['request_id'] = getattr(request, 'request_id', None)
            seen['log_id'] = current_request_id()
            return HttpResponse('ok')

        request = self.factory.get('/listings/')
        if header is not None:
            request.META[REQUEST_ID_META_KEY] = header
        return RequestIdMiddleware(get_response)(request), seen

    def test_echoes_a_valid_client_supplied_id(self):
        response, seen = self._run('trace-42')
        self.assertEqual(response.headers[REQUEST_ID_HEADER], 'trace-42')
        self.assertEqual(seen['request_id'], 'trace-42')
        self.assertEqual(seen['log_id'], 'trace-42')

    def test_generates_an_id_when_the_client_sent_nothing(self):
        response, seen = self._run()
        generated = response.headers[REQUEST_ID_HEADER]
        self.assertRegex(generated, r'^[0-9a-f]{16}$')
        self.assertEqual(seen['request_id'], generated)

    def test_ignores_an_unsafe_client_supplied_id(self):
        response, _ = self._run('evil\nid')
        self.assertRegex(response.headers[REQUEST_ID_HEADER], r'^[0-9a-f]{16}$')

    def test_the_request_id_reaches_the_view(self):
        _, seen = self._run('view-visible')
        self.assertEqual(seen['request_id'], 'view-visible')

    def test_context_is_reset_after_the_request(self):
        self.assertEqual(current_request_id(), NO_REQUEST_ID)
        self._run('leak-check')
        self.assertEqual(current_request_id(), NO_REQUEST_ID)

    def test_context_is_reset_even_when_the_view_raises(self):
        # 漏掉 reset 会把上一个请求的 ID 泄漏给同一个线程里的下一个请求，
        # 关联关系整体错位，而且只有并发时才复现。
        def explode(request):
            raise ValueError('boom')

        with self.assertRaises(ValueError):
            RequestIdMiddleware(explode)(self.factory.get('/'))
        self.assertEqual(current_request_id(), NO_REQUEST_ID)


class RequestScopedLogLineTests(SimpleTestCase):
    """日志行和响应头必须是同一个 ID，否则关联不起来。"""

    def test_a_404_log_line_carries_the_same_id_as_the_response_header(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        # 复刻生产 console handler 的组合：同一个过滤器 + 同一个格式串。
        handler = Capture()
        handler.addFilter(RequestIdFilter())
        handler.setFormatter(logging.Formatter(
            settings.LOGGING['formatters']['structured']['format']))
        logger = logging.getLogger('django.request')
        logger.addHandler(handler)
        try:
            response = self.client.get(
                '/definitely-not-a-page/', HTTP_X_REQUEST_ID='probe-42')
        finally:
            logger.removeHandler(handler)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers[REQUEST_ID_HEADER], 'probe-42')
        self.assertTrue(records, 'django.request 应该记录这次 404')
        for record in records:
            line = handler.format(record)
            self.assertIn('[probe-42]', line)
            self.assertIn('definitely-not-a-page', line)


class LoggingConfigTests(SimpleTestCase):
    """LOGGING 配置自身的不变式。"""

    def setUp(self):
        self.config = settings.LOGGING

    def test_rendering_handlers_carry_the_request_id_filter(self):
        # 少了这个过滤器，格式串里的 %(request_id)s 会在格式化时炸掉，
        # 整行日志丢失——而且只在带该格式串的 handler 上才炸。
        formatters = self.config['formatters']
        missing = []
        for name, handler in sorted(self.config['handlers'].items()):
            fmt = formatters.get(handler.get('formatter'), {}).get('format', '')
            if '%(request_id)s' in fmt and \
                    'request_id' not in handler.get('filters', []):
                missing.append(name)
        self.assertEqual(missing, [], '这些 handler 没有挂 request_id 过滤器')

    def test_mail_admins_does_not_render_the_request_id(self):
        # AdminEmailHandler 用 record.getMessage() 拼主题，不走 formatter，
        # 给它挂 request_id 过滤器只会造成「以为有用」的错觉。
        handler = self.config['handlers']['mail_admins']
        self.assertNotIn('formatter', handler)

    def test_the_console_formatter_renders_the_request_id(self):
        fmt = self.config['formatters']['structured']['format']
        self.assertIn('%(request_id)s', fmt)

    def test_console_logging_is_not_gated_on_debug(self):
        # Django 默认把 console handler 挂在 require_debug_true 上，
        # DEBUG=False 时什么都不往 stdout 写，日志收集器只能收到空气。
        self.assertNotIn(
            'require_debug_true',
            self.config['handlers']['console'].get('filters', []))

    def test_error_mails_still_only_fire_outside_debug(self):
        self.assertIn(
            'require_debug_false',
            self.config['handlers']['mail_admins'].get('filters', []))

    def test_health_probe_stays_quiet_at_info(self):
        # 探针 30 秒一次，INFO 级别会把业务日志整条冲掉。
        logger = self.config['loggers']['campus_glimmer.health']
        self.assertEqual(logger['level'], 'WARNING')

    def test_log_level_is_a_real_logging_level(self):
        self.assertIn(settings.LOG_LEVEL,
                      {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'})

    def test_the_filter_supplies_a_placeholder_outside_a_request(self):
        record = logging.LogRecord(
            'probe', logging.INFO, __file__, 1, 'no request here', None, None)
        self.assertTrue(RequestIdFilter().filter(record))
        self.assertEqual(record.request_id, NO_REQUEST_ID)


class GunicornAccessLogContractTests(SimpleTestCase):
    """entrypoint 里的 gunicorn 参数必须真的能启动容器。"""

    def _gunicorn_command(self):
        text = ENTRYPOINT.read_text(encoding='utf-8')
        self.assertIn('exec gunicorn', text, 'entrypoint 不再 exec gunicorn 了')
        start = text.index('exec gunicorn')
        block = text[start:].replace('\\\n', ' ')
        return shlex.split(block)

    def _options(self):
        command = self._gunicorn_command()
        return {
            command[index]: command[index + 1]
            for index in range(len(command) - 1)
            if command[index].startswith('--')
        }

    def test_every_flag_is_a_real_gunicorn_option(self):
        unknown = sorted(
            flag for flag in self._options() if flag not in GUNICORN_LONG_OPTIONS)
        self.assertEqual(
            unknown, [],
            '这些 gunicorn 选项不存在，容器会以 unrecognized arguments 退出')

    def test_access_logging_is_enabled(self):
        # 没有 --access-logfile，gunicorn 根本不输出 access log，
        # 上面几条断言守着的那条关联链会静默失效。
        self.assertEqual(self._options().get('--access-logfile'), '-')

    def test_access_log_carries_the_request_id(self):
        fmt = self._options()['--access-logformat']
        self.assertIn('%({X-Request-ID}o)s', fmt)

    def test_access_log_carries_the_request_duration(self):
        # 没有任何指标系统之前，慢请求只能从 access log 里看出来。
        self.assertIn('%(L)s', self._options()['--access-logformat'])

    def test_access_log_uses_only_known_gunicorn_atoms(self):
        fmt = self._options()['--access-logformat']
        # gunicorn 的原子字段都是单字母；带花括号的是请求/响应头，另一套语法。
        atoms = set(re.findall(r'%\(([a-zA-Z])\)s', fmt))
        # 头字段的写法是 %({X-Request-ID}o)s：花括号在圆括号里面，
        # 结尾的 s 在右括号之后。写错一层就什么都匹配不到。
        header_atoms = set(re.findall(r'%\(\{([^}]*)\}([io])\)s', fmt))
        self.assertEqual(atoms - GUNICORN_ATOMS, set(),
                         '格式串里有 gunicorn 不认识的原子字段')
        self.assertEqual(header_atoms, {('X-Request-ID', 'o')})

    def test_the_format_is_single_quoted_so_embedded_quotes_survive(self):
        # 格式串本身带双引号，再用双引号包一层会被 shell 吃掉一半，
        # gunicorn 拿到的就是半个参数，直接解析失败。
        for line in ENTRYPOINT.read_text(encoding='utf-8').splitlines():
            if '--access-logformat' in line:
                stripped = line.strip()
                self.assertTrue(
                    stripped.startswith('--access-logformat ' + chr(39)),
                    'access-logformat 必须用单引号包裹：%s' % stripped)
                self.assertTrue(
                    stripped.endswith(chr(39)),
                    'access-logformat 的引号没有闭合：%s' % stripped)
                return
        self.fail('entrypoint 里找不到 --access-logformat')

    def test_entrypoint_keeps_lf_endings(self):
        # 这个文件在 Linux 容器里执行。混进 CRLF 之后 shell 会把参数末尾的
        # 回车当成值的一部分，gunicorn 直接解析失败。
        raw = ENTRYPOINT.read_bytes()
        self.assertNotIn(b'\r', raw, 'entrypoint.sh 混入了 CRLF')

    def test_startup_tasks_still_gate_before_gunicorn(self):
        text = ENTRYPOINT.read_text(encoding='utf-8')
        self.assertIn('${DJANGO_RUN_STARTUP_TASKS:-1}', text)
        self.assertLess(
            text.index('${DJANGO_RUN_STARTUP_TASKS:-1}'),
            text.index('exec gunicorn'))


class LoggingLevelOverrideTests(SimpleTestCase):
    def test_the_default_level_is_visible_in_settings(self):
        with override_settings(LOG_LEVEL='WARNING'):
            self.assertEqual(settings.LOG_LEVEL, 'WARNING')

    def test_the_console_handler_honours_the_configured_level(self):
        self.assertEqual(
            settings.LOGGING['handlers']['console']['level'],
            settings.LOG_LEVEL)
