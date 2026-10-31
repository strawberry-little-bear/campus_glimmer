# campus_glimmer/test_metrics.py
"""指标端点与 Prometheus 暴露格式的契约测试。

这里钉住的不是「能返回 200」，而是四件真会出事的地方：

* **标签基数**。拿 ``request.path`` 当标签值的话，``/listings/1/`` 和
  ``/listings/2/`` 是两个序列，一天就能把 Prometheus 的内存打爆。所以必须
  只写 URL 模板，而且必须真的只用模板——写错一个字段名是看不出来的。
* **暴露格式**。Prometheus 对文本格式的解析并不宽容：标签值里的换行会把
  一行掰成两行，随后整个 target 报解析失败，监控整体瞎掉。所以每条输出行
  都要过一遍严格正则。
* **端点默认关闭**。监控端点暴露的是内部拓扑，忘了关等于把路由表送给外面。
* **多进程语义**。计数器在进程内，多 worker 下每个 worker 各算一份——这件
  事必须在文档里写明，测试在这里只保证它不会假装自己已经解决。
"""

import re
import threading

from django.test import SimpleTestCase, override_settings

from .metrics import (
    LATENCY_BUCKETS,
    MAX_SERIES,
    METRICS_PATH,
    MetricsMiddleware,
    MetricsRegistry,
    get_registry,
    reset_registry,
)

# 一行指标样本：名字{标签} 值。标签段整体可选，值支持整数、小数、科学计数法
# 和直方图的 +Inf。
SAMPLE_RE = re.compile(
    r'^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)'
    r'(?:\{(?P<labels>[^}]*(?:"[^"]*"[^}]*)*)\})?'
    r' (?P<value>-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|\+Inf|NaN)$'
)
# 标签段：若干个 key="value"，value 里的引号和反斜杠必须转义。
LABELS_RE = re.compile(
    r'^(?:[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*")'
    r'(?:,[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*")*$'
)
METADATA_RE = re.compile(r'^# (HELP|TYPE) [a-zA-Z_:][a-zA-Z0-9_:]* .+$')


def parse_exposition(text):
    """把暴露文本拆成 (名字, {标签: 值}, 数值) 的列表，顺带做格式校验。"""

    samples = []
    for line in text.split('\n'):
        if not line or line.startswith('#'):
            if line and not METADATA_RE.match(line):
                raise AssertionError('元数据行不合法：%r' % line)
            continue
        match = SAMPLE_RE.match(line)
        if match is None:
            raise AssertionError('样本行不合法：%r' % line)
        labels = {}
        if match.group('labels'):
            self_labels = match.group('labels')
            if not LABELS_RE.match(self_labels):
                raise AssertionError('标签段不合法：%r' % self_labels)
            for pair in re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', self_labels):
                labels[pair[0]] = pair[1]
        raw = match.group('value')
        if raw in ('+Inf', 'NaN'):
            number = float(raw.replace('+Inf', 'inf'))
        else:
            number = float(raw)
        samples.append((match.group('name'), labels, number))
    return samples


def _metadata_lines(text):
    return len([line for line in text.split('\n') if line.startswith('#')])


def find(samples, name, **labels):
    return [value for sample_name, sample_labels, value in samples
            if sample_name == name
            and all(sample_labels.get(key) == expected for key, expected in labels.items())]


class MetricsRegistryTests(SimpleTestCase):
    def setUp(self):
        self.registry = MetricsRegistry()

    def test_counts_requests_by_method_route_and_status_class(self):
        self.registry.enter_request()
        self.registry.leave_request('GET', 'listings/', 200, 0.01)
        self.registry.enter_request()
        self.registry.leave_request('GET', 'listings/', 404, 0.02)

        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total',
                 route='listings/', method='GET', status_class='2xx'),
            [1],
        )
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total',
                 route='listings/', method='GET', status_class='4xx'),
            [1],
        )

    def test_the_same_route_template_shares_one_series(self):
        # 这是整个指标系统最重要的一条：两条不同的 URL 必须落进同一个序列，
        # 否则基数随时间线性增长，Prometheus 的内存和抓取体积一起失控。
        for pk in (1, 2, 999999):
            self.registry.enter_request()
            self.registry.leave_request('GET', 'listings/<int:pk>/', 200, 0.01)

        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total',
                 route='listings/<int:pk>/'),
            [3],
        )

    def test_the_root_route_is_not_labelled_unmatched(self):
        # 根路径的 ResolverMatch.route 是空串。空着不补的话，访问量最大的
        # 页面会和 404 混进同一个序列。
        self.registry.enter_request()
        self.registry.leave_request('GET', '/', 200, 0.01)
        self.registry.enter_request()
        self.registry.leave_request('GET', 'unmatched', 404, 0.01)

        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='/'),
            [1],
        )
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='unmatched'),
            [1],
        )

    def test_status_codes_are_grouped_into_classes(self):
        for status in (201, 204, 301, 302, 400, 403, 404, 500, 503):
            self.registry.enter_request()
            self.registry.leave_request('GET', 'r', status, 0.01)

        samples = parse_exposition(self.registry.render())
        for expected, count in (('2xx', 2), ('3xx', 2), ('4xx', 3), ('5xx', 2)):
            self.assertEqual(
                find(samples, 'campus_glimmer_http_requests_total',
                     route='r', status_class=expected),
                [count],
                expected,
            )

    def test_an_unclassifiable_status_does_not_crash_the_render(self):
        self.registry.enter_request()
        self.registry.leave_request('GET', 'r', 99, 0.01)
        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', status_class='other'),
            [1],
        )

    def test_in_flight_returns_to_zero_and_can_go_positive(self):
        samples = parse_exposition(self.registry.render())
        self.assertEqual(find(samples, 'campus_glimmer_http_requests_in_flight'), [0])

        self.registry.enter_request()
        self.registry.enter_request()
        samples = parse_exposition(self.registry.render())
        self.assertEqual(find(samples, 'campus_glimmer_http_requests_in_flight'), [2])

        self.registry.leave_request('GET', 'r', 200, 0.01)
        samples = parse_exposition(self.registry.render())
        self.assertEqual(find(samples, 'campus_glimmer_http_requests_in_flight'), [1])

    def test_in_flight_never_goes_negative(self):
        # 少调一次 enter 就多调一次 leave 的话， gauge 会变成负数，
        # 而负的在途请求数是假的，比没有更糟。
        self.registry.leave_request('GET', 'r', 200, 0.01)
        samples = parse_exposition(self.registry.render())
        self.assertEqual(find(samples, 'campus_glimmer_http_requests_in_flight'), [0])

    def test_histogram_buckets_are_cumulative_and_bounded_by_count(self):
        for duration in (0.001, 0.02, 0.4, 3.0, 60.0):
            self.registry.enter_request()
            self.registry.leave_request('GET', 'r', 200, duration)

        samples = parse_exposition(self.registry.render())
        buckets = {
            float('inf') if labels.get('le') == '+Inf' else float(labels['le']): value
            for name, labels, value in samples
            if name == 'campus_glimmer_http_request_duration_seconds_bucket'
            and labels.get('route') == 'r'
        }
        # 0.001 / 0.02 / 0.4 / 3.0 / 60.0 依次落进 0.005、0.025、0.5、5.0 各桶。
        self.assertEqual([buckets[float(upper)] for upper in LATENCY_BUCKETS],
                         [1, 1, 2, 2, 2, 2, 3, 3, 3, 4, 4])

        # +Inf 桶必须等于总数，否则 histogram_quantile 会算错。
        self.assertEqual(buckets[float('inf')], 5)
        self.assertEqual(
            find(samples, 'campus_glimmer_http_request_duration_seconds_count', route='r'),
            [5],
        )
        self.assertAlmostEqual(
            find(samples, 'campus_glimmer_http_request_duration_seconds_sum', route='r')[0],
            0.001 + 0.02 + 0.4 + 3.0 + 60.0,
            places=6,
        )

    def test_histogram_keeps_every_route_separate(self):
        self.registry.enter_request()
        self.registry.leave_request('GET', 'a/', 200, 0.01)
        self.registry.enter_request()
        self.registry.leave_request('GET', 'b/', 200, 0.01)

        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_request_duration_seconds_count', route='a/'),
            [1],
        )
        self.assertEqual(
            find(samples, 'campus_glimmer_http_request_duration_seconds_count', route='b/'),
            [1],
        )

    def test_every_family_declares_help_and_type_exactly_once(self):
        self.registry.enter_request()
        self.registry.leave_request('GET', 'r', 200, 0.01)

        text = self.registry.render()
        lines = text.split('\n')
        names = set()
        for line in lines:
            if line.startswith('# HELP '):
                name = line.split(' ')[2]
                self.assertNotIn(name, names, 'HELP 重复声明：' + name)
                names.add(name)
        for name in names:
            self.assertEqual(
                len([line for line in lines if line.startswith('# TYPE ' + name + ' ')]),
                1,
                name + ' 的 TYPE 不是恰好一行',
            )
        self.assertIn('campus_glimmer_http_requests_total', names)
        self.assertIn('campus_glimmer_http_request_duration_seconds', names)
        self.assertIn('campus_glimmer_http_requests_in_flight', names)
        self.assertIn('campus_glimmer_build_info', names)

    def test_build_info_carries_the_service_name(self):
        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_build_info', service='campus-glimmer'),
            [1],
        )

    def test_process_start_time_is_a_plausible_unix_timestamp(self):
        samples = parse_exposition(self.registry.render())
        started = find(samples, 'campus_glimmer_process_start_time_seconds')[0]
        self.assertGreater(started, 1_600_000_000)

    def test_label_values_with_quotes_and_newlines_stay_on_one_line(self):
        # 标签值里的换行会把一行指标掰成两行，抓取端直接报解析错误。
        # URL 模板由我们自己的 urlconf 生成，本来不该有这些字符；这条测试
        # 保证的是「万一混进来了也不会炸掉整套监控」。
        hostile = 'a"b\\c\nd\re'
        self.registry.enter_request()
        self.registry.leave_request('GET', hostile, 200, 0.01)

        text = self.registry.render()
        # parse_exposition 对每一行都做严格校验：值里混进裸换行时，被掰出来的
        # 那半行匹配不上任何合法形态，这里会直接抛 AssertionError。
        samples = parse_exposition(text)
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', method='GET'), [1],
        )
        # 值里混进裸换行时，被掰出来的那半行既不是元数据也不是合法样本，
        # 总数立刻对不上——这就是这条断言在查的东西。
        lines = [line for line in text.split('\n') if line]
        self.assertEqual(len(lines), len(samples) + _metadata_lines(text))

    def test_a_long_label_value_is_truncated(self):
        self.registry.enter_request()
        self.registry.leave_request('GET', 'x' * 5000, 200, 0.01)
        samples = parse_exposition(self.registry.render())
        matched = [
            value for name, labels, value in samples
            if name == 'campus_glimmer_http_requests_total'
            and labels.get('route') == 'x' * 200
        ]
        self.assertEqual(matched, [1])
        self.assertLess(
            max(len(labels.get('route', '')) for _n, labels, _v in samples), 500)

    def test_series_are_capped_and_the_overflow_is_visible(self):
        registry = MetricsRegistry(max_series=3)
        for index in range(10):
            registry.enter_request()
            registry.leave_request('GET', 'route-%d/' % index, 200, 0.01)

        samples = parse_exposition(registry.render())
        total = sum(find(samples, 'campus_glimmer_http_requests_total'))
        self.assertEqual(total, 10, '封顶不能丢请求')
        self.assertEqual(find(samples, 'campus_glimmer_metrics_label_overflow'), [1])
        # 没被折叠的路由仍然各自成列，被折叠的并进 overflow。
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='route-0/'), [1])
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='overflow'), [7])

    def test_no_overflow_flag_when_the_cap_is_not_reached(self):
        samples = parse_exposition(self.registry.render())
        self.assertEqual(find(samples, 'campus_glimmer_metrics_label_overflow'), [0])

    def test_the_rendered_text_ends_with_a_single_newline(self):
        text = self.registry.render()
        self.assertTrue(text.endswith('\n'))
        self.assertFalse(text.endswith('\n\n'))


class MetricsMiddlewareTests(SimpleTestCase):
    def setUp(self):
        reset_registry()
        self.addCleanup(reset_registry)
        self.registry = get_registry()

    def _middleware(self, get_response):
        return MetricsMiddleware(get_response)

    def test_records_the_route_template_not_the_concrete_path(self):
        from django.test import RequestFactory

        class Response:
            status_code = 200

        def get_response(request):
            request.resolver_match = type('M', (), {'route': 'listings/<int:pk>/'})()
            return Response()

        factory = RequestFactory()
        for pk in (1, 2, 3):
            self._middleware(get_response)(factory.get('/listings/%d/' % pk))

        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total',
                 route='listings/<int:pk>/'),
            [3],
        )

    def test_a_request_without_a_resolver_match_is_recorded_as_unmatched(self):
        from django.test import RequestFactory

        class Response:
            status_code = 200

        def get_response(request):
            return Response()

        self._middleware(get_response)(RequestFactory().get('/nope/'))
        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='unmatched'),
            [1],
        )

    def test_the_root_route_is_recorded_as_a_slash_not_unmatched(self):
        # 上一节那个根路径用例是把 '/' 直接喂给 registry 的，绕过了
        # _route_label；补 '/' 的那一行只在中间件里生效，只有这里能证明。
        from django.test import RequestFactory

        class Response:
            status_code = 200

        def get_response(request):
            request.resolver_match = type('M', (), {'route': ''})()
            return Response()

        self._middleware(get_response)(RequestFactory().get('/'))
        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='/'),
            [1],
        )
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='unmatched'),
            [],
        )

    def test_a_raising_view_is_recorded_as_a_server_error(self):
        # 异常穿透中间件时，客户端看到的就是 5xx。记成 200 会让 5xx 告警
        # 在最需要它的时候安静下来。
        from django.test import RequestFactory

        def get_response(request):
            raise ValueError('boom')

        with self.assertRaises(ValueError):
            self._middleware(get_response)(RequestFactory().get('/boom/'))

        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', status_class='5xx'),
            [1],
        )

    def test_the_metrics_endpoint_does_not_record_itself(self):
        # 抓取自身进指标的话，Prometheus 每次 scrape 都会把请求量和耗时曲线
        # 抬高一点，看板上的数字从此不完全是业务的。
        from django.test import RequestFactory

        class Response:
            status_code = 200

        def get_response(request):
            request.resolver_match = type('M', (), {'route': 'metrics/'})()
            return Response()

        self._middleware(get_response)(RequestFactory().get(METRICS_PATH))
        samples = parse_exposition(self.registry.render())
        self.assertEqual(find(samples, 'campus_glimmer_http_requests_total'), [])

    def test_the_middleware_actually_records_in_the_real_stack(self):
        # 上面几条都用 RequestFactory 手动塞 resolver_match，绕过了真实的中间件
        # 链。这条走 settings.MIDDLEWARE 的完整栈：URL 解析发生在链的最内层，
        # 只有真的请求一次，才能证明「finally 里取路由」在真实栈上成立，也证明
        # 这个中间件确实挂在链里，而不是只在自己被单独实例化时才工作。
        self.client.get('/healthz/')
        samples = parse_exposition(get_registry().render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='healthz/'),
            [1],
        )
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', status_class='2xx'),
            [1],
        )

    def test_concurrent_requests_do_not_lose_counts(self):
        from django.test import RequestFactory

        class Response:
            status_code = 200

        def get_response(request):
            request.resolver_match = type('M', (), {'route': 'r/'})()
            return Response()

        middleware = self._middleware(get_response)
        factory = RequestFactory()
        threads = [
            threading.Thread(target=middleware, args=(factory.get('/r/'),))
            for _ in range(40)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        samples = parse_exposition(self.registry.render())
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='r/'), [40])
        self.assertEqual(find(samples, 'campus_glimmer_http_requests_in_flight'), [0])

    def test_in_flight_is_positive_while_a_request_is_running(self):
        from django.test import RequestFactory

        seen = {}

        def get_response(request):
            samples = parse_exposition(self.registry.render())
            seen['in_flight'] = find(
                samples, 'campus_glimmer_http_requests_in_flight')[0]
            request.resolver_match = type('M', (), {'route': 'r/'})()
            return type('R', (), {'status_code': 200})()

        self._middleware(get_response)(RequestFactory().get('/r/'))
        self.assertEqual(seen['in_flight'], 1)


@override_settings(METRICS_TOKEN='top-secret-token')
class MetricsEndpointAuthTests(SimpleTestCase):
    def setUp(self):
        reset_registry()
        self.addCleanup(reset_registry)

    def test_rejects_a_request_without_a_token(self):
        self.assertEqual(self.client.get(METRICS_PATH).status_code, 404)

    def test_rejects_a_wrong_token(self):
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='Bearer nope')
        self.assertEqual(response.status_code, 404)

    def test_rejects_a_non_bearer_scheme(self):
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='Basic top-secret-token')
        self.assertEqual(response.status_code, 404)

    def test_accepts_the_configured_token(self):
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='Bearer top-secret-token')
        self.assertEqual(response.status_code, 200)

    def test_the_bearer_scheme_is_case_insensitive(self):
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='bearer top-secret-token')
        self.assertEqual(response.status_code, 200)

    def test_surrounding_whitespace_is_tolerated(self):
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='Bearer   top-secret-token  ')
        self.assertEqual(response.status_code, 200)

    def test_a_token_with_non_ascii_bytes_does_not_raise(self):
        # compare_digest 只接受同为 str（且仅 ASCII）或同为 bytes。直接把
        # 客户端来的字符串传进去，遇到非 ASCII 会抛 TypeError，把一次
        # 未授权抓取变成一次 500。
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='Bearer \u8bf7\u6c42\u6807\u8bc6')
        self.assertEqual(response.status_code, 404)

    def test_a_token_is_not_prefix_matched(self):
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='Bearer top-secret')
        self.assertEqual(response.status_code, 404)

    def test_the_token_comparison_goes_through_compare_digest(self):
        # 用 == 比令牌会在第一个不同的字节处返回，等于把令牌泄露给会计时的
        # 攻击者。行为上观测不到这一点，只能钉住实现方式：谁把它换回 ==，
        # 这个测试先红。顺带钉住两边都是 bytes——直接传客户端来的字符串会在
        # 遇到非 ASCII 令牌时抛 TypeError，把一次未授权抓取变成 500。
        from django.test import RequestFactory

        from . import metrics as metrics_module
        from .metrics import _authorised

        calls = []

        class FakeHmac:
            def compare_digest(self, left, right):
                calls.append((type(left).__name__, type(right).__name__))
                return True

        real_hmac = metrics_module.hmac
        metrics_module.hmac = FakeHmac()
        try:
            request = RequestFactory().get(
                METRICS_PATH, HTTP_AUTHORIZATION='Bearer sekrit-token',
            )
            with override_settings(METRICS_TOKEN='sekrit-token'):
                self.assertTrue(_authorised(request))
        finally:
            metrics_module.hmac = real_hmac

        self.assertEqual(calls, [('bytes', 'bytes')])

    def test_only_get_and_head_are_allowed(self):
        response = self.client.post(
            METRICS_PATH, HTTP_AUTHORIZATION='Bearer top-secret-token')
        self.assertEqual(response.status_code, 405)

    def test_the_response_is_prometheus_text_and_not_cacheable(self):
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='Bearer top-secret-token')
        self.assertEqual(
            response['Content-Type'], 'text/plain; version=0.0.4; charset=utf-8')
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_the_response_parses_as_a_valid_exposition(self):
        self.client.get('/healthz/')
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='Bearer top-secret-token')
        samples = parse_exposition(response.content.decode('utf-8'))
        names = {name for name, _labels, _value in samples}
        self.assertIn('campus_glimmer_http_requests_total', names)
        self.assertIn('campus_glimmer_http_request_duration_seconds_bucket', names)
        self.assertIn('campus_glimmer_http_request_duration_seconds_sum', names)
        self.assertIn('campus_glimmer_http_request_duration_seconds_count', names)
        self.assertIn('campus_glimmer_build_info', names)

    def test_a_scrape_is_not_counted_as_a_request(self):
        self.client.get(METRICS_PATH, HTTP_AUTHORIZATION='Bearer top-secret-token')
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='Bearer top-secret-token')
        samples = parse_exposition(response.content.decode('utf-8'))
        self.assertEqual(find(samples, 'campus_glimmer_http_requests_total'), [])


@override_settings(METRICS_TOKEN='')
class MetricsEndpointDisabledByDefaultTests(SimpleTestCase):
    def test_the_endpoint_is_invisible_without_a_token(self):
        self.assertEqual(self.client.get(METRICS_PATH).status_code, 404)

    def test_a_supplied_token_cannot_resurrect_it(self):
        response = self.client.get(
            METRICS_PATH, HTTP_AUTHORIZATION='Bearer anything')
        self.assertEqual(response.status_code, 404)

    def test_the_url_is_reachable_by_name(self):
        from django.urls import reverse
        self.assertEqual(reverse('metrics'), METRICS_PATH)


class MetricsSettingsTests(SimpleTestCase):
    def test_the_token_defaults_to_empty(self):
        from django.conf import settings
        with override_settings(METRICS_TOKEN=''):
            self.assertEqual(settings.METRICS_TOKEN, '')

    def test_the_default_max_series_is_a_sane_bound(self):
        # 上限的意义在于「宁可丢精度也不能丢可用性」。太小会把正常路由挤掉，
        # 太大则失去封顶的意义。
        self.assertGreater(MAX_SERIES, 100)
        self.assertLess(MAX_SERIES, 5000)

    def test_reset_registry_replaces_the_container(self):
        first = get_registry()
        second = reset_registry()
        self.assertIsNot(first, second)
        self.assertIs(get_registry(), second)
