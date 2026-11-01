# -*- coding: utf-8 -*-
# campus_glimmer/test_alerts.py
"""按指标的阈值告警的契约测试。

这里钉住的不是「能返回一份 JSON」，而是四件真会出事的地方：

* **看板和告警必须是同一个数**。规则读的是 ``iter_samples`` 摊出来的样本，
  也就是 ``/metrics/`` 正要暴露出去的那一批。两侧一旦分裂，症状是某条规则
  永远不触发——和「没有这条规则」在看板上长得一模一样，没有任何报错。
* **pending → firing 的状态机**。一次抖动不该喊人，一次持续故障必须喊。
  ``for`` 写短了天天狼来了，写长了故障已经半小时看板上还是 pending。
* **没有数据时不许下结论**。序列不存在、样本不足、取不到基线，都必须退回
  「不判断」而不是当成 0：把「没有这个序列」读成「这个序列是 0」，会在监控
  自己坏掉的时候给出一条一切正常的结论。
* **告警自己坏掉不能带走监控端点**。评估抛异常时 ``/metrics/`` 仍然得 200，
  否则一个挂掉的监控比它要报告的故障更难排查。
"""

import json
import math
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from .alerts import (
    METRIC_BUCKET_MISMATCH,
    METRIC_DURATION_BUCKET,
    METRIC_INSTANCES,
    METRIC_LABEL_OVERFLOW,
    METRIC_READ_ERRORS,
    METRIC_REQUESTS_TOTAL,
    METRIC_VIEW_AGE,
    RULES,
    AlertEngine,
    Rule,
    RuleContext,
    SampleWindow,
    _probe_bucket_mismatch,
    _probe_error_rate,
    _probe_latency,
    _probe_label_overflow,
    _probe_read_errors,
    _probe_view_stale,
    get_engine,
    histogram_quantile,
    render_alerts,
    reset_engine,
)
from .metrics import (
    ALERTS_PATH,
    METRICS_PATH,
    get_registry,
    iter_samples,
    reset_registry,
)
from .test_metrics import find, parse_exposition

TOKEN = 'top-secret-token'
AUTH = {'HTTP_AUTHORIZATION': 'Bearer ' + TOKEN}


class _FakeProbe:
    """一个能被测试翻转的探针：状态机的用例需要精确控制条件何时成立。"""

    def __init__(self, on=True):
        self.on = on

    def __call__(self, _context):
        return self.on, 1.0, '测试条件'


def _gauge(name, value):
    return (name, {}, float(value))


def _requests(count, status_class='2xx', route='r/', method='GET'):
    return (
        METRIC_REQUESTS_TOTAL,
        {'route': route, 'method': method, 'status_class': status_class},
        float(count),
    )


def _read_errors(count):
    return (METRIC_READ_ERRORS, {}, float(count))


def _bucket_samples(cumulative):
    """按累计条数造一组直方图样本。

    ``cumulative`` 是 ``[(上界, 累计条数), ...]``，最后一项的上界是
    ``math.inf``，值即总样本数——Prometheus 要求直方图必须带 +Inf 桶。
    """

    return [
        (
            METRIC_DURATION_BUCKET,
            {
                'method': 'GET',
                'route': 'r/',
                'le': '+Inf' if upper == math.inf else repr(float(upper)),
            },
            float(count),
        )
        for upper, count in cumulative
    ]


def _context(samples, now=1000.0, window=None):
    return RuleContext(samples, SampleWindow() if window is None else window, now)


def _family(samples, family):
    """某一族告警序列上出现的 alertname，按名字排序。

    按名字而不是按值比对：firing/pending 两族的取值都是 1，光看数值分不出
    一条规则到底属于哪一族。
    """

    return sorted(
        labels['alertname']
        for name, labels, _value in samples
        if name == family
    )


def _states_by_state(engine, state):
    return sorted(item['name'] for item in engine.states()
                  if item['state'] == state)


class HistogramQuantileTests(SimpleTestCase):
    """分位数算错不会报错，只会让延迟告警在错误的时刻响或者不响。"""

    def test_no_buckets_at_all_makes_no_claim(self):
        # 返回 None 而不是 0：把「没有数据」显示成「0 秒」会让看板看起来很快。
        self.assertIsNone(histogram_quantile({}, 0.95))

    def test_a_quantile_of_zero_lands_on_the_first_empty_bucket(self):
        # 空档取上界，不除零。这一条同时钉住 count == lower_count 那条分支：
        # 去掉它，这个用例会抛 ZeroDivisionError。
        self.assertEqual(
            histogram_quantile({0.1: 0, 1.0: 5, math.inf: 5}, 0.0), 0.1)

    def test_the_median_lands_exactly_on_a_full_bucket(self):
        self.assertEqual(
            histogram_quantile({0.1: 50, 1.0: 100, math.inf: 100}, 0.5), 0.1)

    def test_the_p95_interpolates_inside_the_bucket(self):
        self.assertAlmostEqual(
            histogram_quantile({0.1: 50, 1.0: 100, math.inf: 100}, 0.95),
            0.91, places=6,
        )

    def test_without_the_infinity_bucket_it_falls_back_to_the_largest_edge(self):
        # +Inf 桶缺失时只能拿最大的有限上界当总数：低估可以接受，
        # 凭空造一个总数不行。
        self.assertAlmostEqual(
            histogram_quantile({0.1: 1, 1.0: 2, 10.0: 5}, 0.95), 9.25, places=6)

    def test_zero_samples_make_no_claim(self):
        self.assertIsNone(histogram_quantile({0.1: 0, math.inf: 0}, 0.95))


class AlertStateMachineTests(SimpleTestCase):
    """pending → firing：一次抖动不喊人，持续故障必须喊。"""

    def _engine(self, for_seconds):
        self.probe = _FakeProbe()
        return AlertEngine(
            rules=[Rule('Probe', 'warning', '测试规则', for_seconds, self.probe)],
            window=SampleWindow(max_age=3600),
        )

    def test_a_condition_inside_the_for_window_stays_pending(self):
        engine = self._engine(60)
        self.assertEqual(engine.evaluate([], now=0.0)[0]['state'], 'pending')
        self.assertEqual(engine.evaluate([], now=30.0)[0]['state'], 'pending')

    def test_the_condition_fires_only_once_the_for_window_has_passed(self):
        engine = self._engine(60)
        engine.evaluate([], now=0.0)
        state = engine.evaluate([], now=60.0)[0]
        self.assertEqual(state['state'], 'firing')
        # 持续时长从条件成立那一刻算起，不是从升级那一刻：值班的人问的是
        # 「坏了多久」，不是「这张告警贴了多久」。
        self.assertEqual(state['since'], 0.0)
        self.assertEqual(state['active_seconds'], 60.0)

    def test_a_blip_that_clears_early_never_fires_and_restarts_the_clock(self):
        engine = self._engine(60)
        engine.evaluate([], now=0.0)
        self.probe.on = False
        self.assertEqual(engine.evaluate([], now=30.0)[0]['state'], 'inactive')
        self.probe.on = True
        # 重新越界必须重新熬满 for。继承上一次的 pending_since 会让一次抖动
        # 在几分钟后被算成持续故障。
        self.assertEqual(engine.evaluate([], now=31.0)[0]['state'], 'pending')
        self.assertEqual(engine.evaluate([], now=91.0)[0]['state'], 'firing')

    def test_recovery_clears_the_timers_and_the_active_seconds(self):
        engine = self._engine(10)
        engine.evaluate([], now=0.0)
        self.assertEqual(engine.evaluate([], now=10.0)[0]['state'], 'firing')
        self.probe.on = False
        state = engine.evaluate([], now=500.0)[0]
        self.assertEqual(state['state'], 'inactive')
        self.assertIsNone(state['since'])
        self.assertEqual(state['active_seconds'], 0.0)

    def test_states_are_handed_out_as_copies(self):
        # 调用方改返回值不能改动引擎：/metrics/ 和 /alerts/ 都会拿这份列表，
        # 任一侧的整理不该变成另一侧的真相。
        engine = self._engine(0)
        engine.evaluate([], now=0.0)
        states = engine.states()
        states[0]['state'] = 'tampered'
        self.assertEqual(engine.states()[0]['state'], 'firing')

    def test_every_rule_is_evaluated_on_its_own(self):
        engine = AlertEngine(rules=[
            Rule('Quiet', 'info', '不响', 0, _FakeProbe(on=False)),
            Rule('Loud', 'critical', '会响', 0, _FakeProbe(on=True)),
        ])
        states = engine.evaluate([], now=0.0)
        self.assertEqual([s['name'] for s in states], ['Quiet', 'Loud'])
        self.assertEqual([s['state'] for s in states], ['inactive', 'firing'])


class ViewAndSharedProbeTests(SimpleTestCase):
    """视图新鲜度、共享降级与基数：这些数字错了不会有人来报障。"""

    def test_a_missing_age_series_makes_no_claim(self):
        fired, value, detail = _probe_view_stale(_context([]))
        self.assertFalse(fired)
        self.assertIsNone(value)
        self.assertIn('无法判断', detail)

    def test_a_fresh_view_stays_quiet(self):
        fired, value, _detail = _probe_view_stale(
            _context([_gauge(METRIC_VIEW_AGE, 5.0)]))
        self.assertFalse(fired)
        self.assertEqual(value, 5.0)

    def test_a_stale_view_fires(self):
        fired, value, detail = _probe_view_stale(
            _context([_gauge(METRIC_VIEW_AGE, 60.0)]))
        self.assertTrue(fired)
        self.assertEqual(value, 60.0)
        self.assertIn('60', detail)

    def test_read_errors_without_a_baseline_make_no_claim(self):
        # 窗口里只有一次样本时算不出增量。把「算不出」当成「没有失败」，
        # 会让刚接上监控的那段时间看起来一切正常。
        window = SampleWindow()
        window.record([_read_errors(3)], now=1000.0)
        fired, value, _detail = _probe_read_errors(
            _context([], now=1000.0, window=window))
        self.assertFalse(fired)
        self.assertIsNone(value)

    def test_a_few_read_errors_stay_quiet(self):
        window = SampleWindow()
        window.record([_read_errors(0)], now=0.0)
        window.record([_read_errors(2)], now=1000.0)
        fired, value, _detail = _probe_read_errors(
            _context([], now=1000.0, window=window))
        self.assertFalse(fired)
        self.assertEqual(value, 2)

    def test_rising_read_errors_fire(self):
        window = SampleWindow()
        window.record([_read_errors(0)], now=0.0)
        window.record([_read_errors(5)], now=1000.0)
        fired, value, detail = _probe_read_errors(
            _context([], now=1000.0, window=window))
        self.assertTrue(fired)
        self.assertEqual(value, 5)
        self.assertIn('退化', detail)

    def test_a_bucket_mismatch_fires_and_zero_stays_quiet(self):
        self.assertTrue(
            _probe_bucket_mismatch(_context([_gauge(METRIC_BUCKET_MISMATCH, 2)]))[0])
        fired, value, _detail = _probe_bucket_mismatch(
            _context([_gauge(METRIC_BUCKET_MISMATCH, 0)]))
        self.assertFalse(fired)
        self.assertEqual(value, 0)

    def test_label_overflow_fires_and_a_missing_series_stays_quiet(self):
        self.assertTrue(
            _probe_label_overflow(_context([_gauge(METRIC_LABEL_OVERFLOW, 1)]))[0])
        # 没有这个序列时不是 0，而是「不知道」：结论同样是不响，但理由不同。
        fired, value, _detail = _probe_label_overflow(_context([]))
        self.assertFalse(fired)
        self.assertEqual(value, 0)


class TrafficAndLatencyProbeTests(SimpleTestCase):
    """错误率与延迟：低流量时段最容易在这里骗人或被骗。"""

    def _window(self, baseline, current, gap=1000.0):
        window = SampleWindow()
        window.record(baseline, now=0.0)
        window.record(current, now=gap)
        return window

    def test_too_few_samples_make_no_claim_about_the_error_rate(self):
        window = self._window([_requests(0)], [_requests(50)])
        fired, value, detail = _probe_error_rate(
            _context([], now=1000.0, window=window))
        self.assertFalse(fired)
        self.assertEqual(value, 50)
        self.assertIn('样本不足', detail)

    def test_a_low_error_rate_stays_quiet(self):
        window = self._window(
            [_requests(0), _requests(0, status_class='5xx')],
            [_requests(1000), _requests(10, status_class='5xx')],
        )
        fired, value, _detail = _probe_error_rate(
            _context([], now=1000.0, window=window))
        self.assertFalse(fired)
        # 分母是含 5xx 的请求总数（1010），不是 2xx 的 1000：占比的分母
        # 本来就该是全部请求。写成 1000 会让阈值悄悄变严。
        self.assertAlmostEqual(value, 10 / 1010)

    def test_a_high_error_rate_fires(self):
        window = self._window(
            [_requests(0), _requests(0, status_class='5xx')],
            [_requests(1000), _requests(200, status_class='5xx')],
        )
        fired, value, detail = _probe_error_rate(
            _context([], now=1000.0, window=window))
        self.assertTrue(fired)
        self.assertAlmostEqual(value, 200 / 1200)
        self.assertIn('错误页', detail)

    def test_too_few_requests_make_no_claim_about_latency(self):
        window = self._window(
            _bucket_samples([(0.005, 0), (0.01, 0), (math.inf, 0)]),
            _bucket_samples([(0.005, 10), (0.01, 10), (math.inf, 10)]),
        )
        fired, value, detail = _probe_latency(
            _context([], now=1000.0, window=window))
        self.assertFalse(fired)
        self.assertIsNone(value)
        self.assertIn('样本不足', detail)

    def test_a_fast_p95_stays_quiet(self):
        window = self._window(
            _bucket_samples([(0.1, 0), (1.0, 0), (math.inf, 0)]),
            _bucket_samples([(0.1, 100), (1.0, 100), (math.inf, 100)]),
        )
        fired, value, _detail = _probe_latency(
            _context([], now=1000.0, window=window))
        self.assertFalse(fired)
        self.assertAlmostEqual(value, 0.095, places=6)

    def test_a_slow_p95_fires(self):
        window = self._window(
            _bucket_samples([(0.1, 0), (1.0, 0), (5.0, 0), (math.inf, 0)]),
            _bucket_samples([(0.1, 0), (1.0, 0), (5.0, 100), (math.inf, 100)]),
        )
        fired, value, detail = _probe_latency(
            _context([], now=1000.0, window=window))
        self.assertTrue(fired)
        self.assertAlmostEqual(value, 4.8, places=6)
        self.assertIn('p95', detail)


class SampleWindowTests(SimpleTestCase):
    """窗口是规则唯一的历史来源：算错增量比没有历史更糟。"""

    def test_a_single_record_has_no_baseline_to_compare_against(self):
        window = SampleWindow()
        window.record([_requests(7)], now=100.0)
        self.assertIsNone(
            window.increase(METRIC_REQUESTS_TOTAL, 600, now=100.0))

    def test_increase_measures_the_difference_across_the_window(self):
        window = SampleWindow()
        window.record([_requests(10)], now=0.0)
        window.record([_requests(35)], now=100.0)
        self.assertEqual(window.increase(METRIC_REQUESTS_TOTAL, 600, now=100.0), 25)

    def test_a_counter_that_went_backwards_counts_as_zero(self):
        # 进程重启会让计数器归零。算成负增量会在看板上划出一条向下的假跌，
        # 比算成 0 更难排查。
        window = SampleWindow()
        window.record([_requests(100)], now=0.0)
        window.record([_requests(4)], now=100.0)
        self.assertEqual(window.increase(METRIC_REQUESTS_TOTAL, 600, now=100.0), 0)

    def test_peak_ignores_samples_older_than_the_window(self):
        window = SampleWindow()
        window.record([_gauge(METRIC_INSTANCES, 5)], now=0.0
                       )
        window.record([_gauge(METRIC_INSTANCES, 2)], now=100.0)
        self.assertEqual(window.peak(METRIC_INSTANCES, 600, now=100.0), 5)
        # 一小时前的峰值不该被记住：正常的缩容和早已结束的扩容都会被算成副本丢失。
        self.assertEqual(window.peak(METRIC_INSTANCES, 10, now=100.0), 2)

    def test_samples_older_than_the_max_age_are_pruned(self):
        # 窗口必须是有界的：跟着进程一起无限长大的结构，本身就是一个故障。
        window = SampleWindow(max_age=60)
        window.record([_gauge(METRIC_INSTANCES, 5)], now=0.0)
        window.record([_gauge(METRIC_INSTANCES, 2)], now=100.0)
        self.assertEqual(window.peak(METRIC_INSTANCES, 3600, now=100.0), 2)


@override_settings(METRICS_TOKEN=TOKEN)
class MetricsExposureTests(SimpleTestCase):
    """告警状态在 /metrics/ 上的形状，以及与看板口径的一致性。"""

    def setUp(self):
        reset_registry()
        reset_engine()
        self.addCleanup(reset_registry)
        self.addCleanup(reset_engine)

    def test_the_alert_families_declare_help_and_type_exactly_once(self):
        text = self.client.get(METRICS_PATH, **AUTH).content.decode('utf-8')
        declared = [
            line.split()[2] for line in text.split('\n')
            if line.startswith('# TYPE ')
        ]
        self.assertEqual(len(declared), len(set(declared)))
        for name in ('campus_glimmer_alert_firing', 'campus_glimmer_alert_pending',
                     'campus_glimmer_alert_active_seconds',
                     'campus_glimmer_alert_evaluations_total',
                     'campus_glimmer_alert_evaluation_errors_total'):
            self.assertIn(name, declared)

    def test_the_alert_rules_read_the_same_names_the_endpoint_exposes(self):
        # 本模块唯一比「算法正确」更要紧的不变式。规则的输入是 iter_samples
        # 摊出来的样本，也就是 /metrics/ 正要暴露出去的那一批；两边名字一旦
        # 分裂，症状是某条规则永远不触发——和「没有这条规则」在看板上长得
        # 一模一样，而单行格式校验照样全绿。
        registry = get_registry()
        registry.enter_request()
        registry.leave_request('GET', 'healthz/', 200, 0.01)
        view = registry.snapshot()

        rendered = {
            name for name, _labels, _value in parse_exposition(registry.render(view))
        }
        exposed = {name for name, _labels, _value in iter_samples(view)}
        self.assertEqual(exposed, rendered)
        # 直方图少了 _bucket 后缀的话，这一条先红。
        self.assertTrue(METRIC_DURATION_BUCKET.endswith('_bucket'))
        self.assertIn(METRIC_DURATION_BUCKET, rendered)

    def test_a_firing_alert_is_visible_in_the_exposition(self):
        samples = parse_exposition(render_alerts([_gauge(METRIC_LABEL_OVERFLOW, 1.0)]))
        self.assertEqual(
            find(samples, 'campus_glimmer_alert_firing',
                 alertname='MetricsLabelOverflow', severity='warning'),
            [1],
        )
        # 归属也要钉住，不能只按 alertname 找：把别的状态的规则一起塞进 firing
        # 家族的改动，会让上面这条照样返回 [1]。两族和状态机一旦对不上，
        # 症状是看板把一条没响的画成正在响，而所有数值本身毫无异常。
        self.assertEqual(
            _family(samples, 'campus_glimmer_alert_firing'),
            _states_by_state(get_engine(), 'firing'),
        )
        self.assertEqual(
            _family(samples, 'campus_glimmer_alert_pending'),
            _states_by_state(get_engine(), 'pending'),
        )

    def test_a_pending_alert_is_not_reported_as_firing(self):
        # for 时长没熬满就必须停在 pending。把 pending 也报成 firing 的后果是
        # 每次抖动都喊人，一周之后没人再看告警。
        samples = parse_exposition(render_alerts([_gauge(METRIC_BUCKET_MISMATCH, 1.0)]))
        self.assertEqual(
            find(samples, 'campus_glimmer_alert_pending',
                 alertname='MetricsBucketMismatch'),
            [1],
        )
        self.assertEqual(find(samples, 'campus_glimmer_alert_firing'), [])
        # 反过来也要按状态机逐条对上：把过滤条件从 == 'pending' 放宽成
        # != 'inactive' 时，pending 家族会同时装进 pending 和 firing 两类，
        # 而上面两条按 alertname 的断言照样都能通过。
        self.assertEqual(
            _family(samples, 'campus_glimmer_alert_pending'),
            _states_by_state(get_engine(), 'pending'),
        )
        self.assertEqual(
            _family(samples, 'campus_glimmer_alert_firing'),
            _states_by_state(get_engine(), 'firing'),
        )
        # active_seconds 只描述「还没结束的」故障：inactive 的规则没有在持续的
        # 状态，报出来只会让人以为它刚恢复过。
        self.assertEqual(
            _family(samples, 'campus_glimmer_alert_active_seconds'),
            _states_by_state(get_engine(), 'pending')
            + _states_by_state(get_engine(), 'firing'),
        )

    def test_the_evaluation_counters_advance_with_every_scrape(self):
        render_alerts([])
        render_alerts([])
        samples = parse_exposition(render_alerts([]))
        self.assertEqual(find(samples, 'campus_glimmer_alert_evaluations_total'), [3])
        self.assertEqual(
            find(samples, 'campus_glimmer_alert_evaluation_errors_total'), [0])

    def test_the_alerts_are_appended_after_the_business_metrics(self):
        # 人从前往后读：先看业务数字，再看监控自己的状态。
        self.client.get('/healthz/')
        text = self.client.get(METRICS_PATH, **AUTH).content.decode('utf-8')
        declared = [
            line.split()[2] for line in text.split('\n')
            if line.startswith('# TYPE ')
        ]
        self.assertEqual(declared[-5:], [
            'campus_glimmer_alert_firing',
            'campus_glimmer_alert_pending',
            'campus_glimmer_alert_active_seconds',
            'campus_glimmer_alert_evaluations_total',
            'campus_glimmer_alert_evaluation_errors_total',
        ])


@override_settings(METRICS_TOKEN=TOKEN)
class AlertsEndpointTests(SimpleTestCase):
    """/alerts/ 是给人看的同一个数的另一种形状。"""

    def setUp(self):
        reset_registry()
        reset_engine()
        self.addCleanup(reset_registry)
        self.addCleanup(reset_engine)

    def test_without_a_token_the_endpoint_is_not_there(self):
        self.assertEqual(self.client.get(ALERTS_PATH).status_code, 404)

    def test_only_get_and_head_are_allowed(self):
        self.assertEqual(self.client.post(ALERTS_PATH, **AUTH).status_code, 405)

    def test_the_payload_carries_the_service_identity_and_the_counts(self):
        response = self.client.get(ALERTS_PATH, **AUTH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEqual(response['Cache-Control'], 'no-store')
        payload = json.loads(response.content.decode('utf-8'))
        self.assertEqual(payload['service'], settings.METRICS_SERVICE_NAME)
        self.assertIn('generated_at', payload)
        counts = payload['counts']
        self.assertEqual(
            counts['firing'] + counts['pending'] + counts['inactive'],
            len(payload['alerts']),
        )

    def test_every_rule_is_reported_even_when_it_is_quiet(self):
        # 只报 firing 的话，读的人分不出「这条规则不存在」和「这条规则没响」。
        payload = json.loads(
            self.client.get(ALERTS_PATH, **AUTH).content.decode('utf-8'))
        self.assertEqual(
            sorted(alert['name'] for alert in payload['alerts']),
            sorted(rule.name for rule in RULES),
        )

    def test_the_payload_agrees_with_the_metrics_endpoint(self):
        # 两个端点回答的是同一批问题：JSON 里的 firing 集合必须和
        # campus_glimmer_alert_firing 上的标签集合逐条对上。
        payload = json.loads(
            self.client.get(ALERTS_PATH, **AUTH).content.decode('utf-8'))
        samples = parse_exposition(
            self.client.get(METRICS_PATH, **AUTH).content.decode('utf-8'))
        firing = {
            labels['alertname']
            for name, labels, _value in samples
            if name == 'campus_glimmer_alert_firing'
        }
        self.assertEqual(
            firing,
            {alert['name'] for alert in payload['alerts'] if alert['state'] == 'firing'},
        )


@override_settings(METRICS_TOKEN=TOKEN)
class AlertSelfPreservationTests(SimpleTestCase):
    """监控自己出事时的表现，比它顺风顺水时的表现更值得钉住。"""

    def setUp(self):
        reset_registry()
        reset_engine()
        self.addCleanup(reset_registry)
        self.addCleanup(reset_engine)

    def test_a_broken_probe_does_not_take_the_endpoint_down(self):
        # 一个 500 的监控端点比它要报告的故障更难排查：Prometheus 会记
        # up == 0，而看板上所有曲线一起消失。
        engine = get_engine()
        with patch.object(engine, '_rules', (Rule('Broken', 'warning', '测试规则', 0,
                                                  lambda _c: 1 / 0),)):
            response = self.client.get(METRICS_PATH, **AUTH)
        self.assertEqual(response.status_code, 200)
        samples = parse_exposition(response.content.decode('utf-8'))
        self.assertEqual(
            find(samples, 'campus_glimmer_alert_evaluation_errors_total'), [1])

    def test_a_failed_evaluation_keeps_the_previous_conclusion(self):
        engine = get_engine()
        loud = Rule('Loud', 'warning', '测试规则', 0, _FakeProbe(on=True))
        with patch.object(engine, '_rules', (loud,)):
            engine.evaluate([], now=0.0)
            self.assertEqual(engine.states()[0]['state'], 'firing')
            with patch.object(
                engine, '_rules', (Rule('Broken', 'warning', '测试规则', 0,
                                        lambda _c: 1 / 0),)):
                states = engine.evaluate([], now=1.0)
        # 沿用上一次的结论，而不是清空：清空会让一次评估抖动把正在响的告警
        # 从看板上抹掉，值班的人据此判断「已经恢复了」。
        self.assertEqual([s['name'] for s in states], ['Loud'])
        self.assertEqual(states[0]['state'], 'firing')

    def test_scraping_the_alerts_endpoint_does_not_feed_the_metrics(self):
        # 观测自己的请求不进指标，否则每次巡检都把请求量曲线抬高一点，
        # 看板上的数字从此不完全是自己业务的。
        self.client.get(ALERTS_PATH, **AUTH)
        samples = parse_exposition(
            self.client.get(METRICS_PATH, **AUTH).content.decode('utf-8'))
        self.assertEqual(find(samples, METRIC_REQUESTS_TOTAL), [])

    def test_the_engine_is_a_singleton_that_tests_can_reset(self):
        # 单例是为了让「持续了多久」跨抓取累积；能重置是为了测试之间
        # 不互相传染——否则先跑的用例留下的 pending 会让后跑的用例失真。
        first = get_engine()
        self.assertIs(get_engine(), first)
        replaced = reset_engine()
        self.assertIsNot(replaced, first)
        self.assertIs(get_engine(), replaced)