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
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings

from .alerts import (
    ALERT_HISTORY_LIMIT,
    METRIC_BUCKET_MISMATCH,
    METRIC_DURATION_BUCKET,
    METRIC_INSTANCES,
    METRIC_LABEL_OVERFLOW,
    METRIC_READ_ERRORS,
    METRIC_REQUESTS_TOTAL,
    METRIC_VIEW_AGE,
    RULES,
    AlertEngine,
    AlertNotifier,
    Rule,
    RuleContext,
    SampleWindow,
    _probe_bucket_mismatch,
    _probe_error_rate,
    _probe_latency,
    _probe_label_overflow,
    _probe_read_errors,
    _probe_view_stale,
    _alert_families,
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
    render_families,
    reset_registry,
)
from .test_metrics import find, parse_exposition
from observability.history import DB_HISTORY_LIMIT, DatabaseAlertHistory
from observability.models import AlertTransition

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
@override_settings(ALERT_HISTORY_DB_ENABLED=False)
class AlertNotificationTests(SimpleTestCase):
    """Webhook 只在状态翻转时发一次，且它的故障不能外溢。"""

    def _response(self, status=204):
        response = MagicMock()
        response.status = status
        response.__enter__.return_value = response
        return response

    def _engine(self, notifier, *rules):
        return AlertEngine(rules=rules, notifier=notifier)

    def test_pending_does_not_notify(self):
        notifier = MagicMock()
        engine = self._engine(
            notifier,
            Rule('Pending', 'warning', '测试规则', 10, _FakeProbe(on=True)),
        )
        self.assertEqual(engine.evaluate([], now=0.0)[0]['state'], 'pending')
        notifier.deliver.assert_not_called()
        self.assertEqual(engine.counters()['notifications']['skipped'], 0)

    def test_firing_notifies_once_and_ongoing_firing_does_not_repeat(self):
        notifier = MagicMock()
        notifier.deliver.return_value = {
            'delivered': 1, 'failed': 0, 'skipped': 0,
        }
        engine = self._engine(
            notifier,
            Rule('Firing', 'critical', '测试规则', 0, _FakeProbe(on=True)),
        )
        engine.evaluate([], now=0.0)
        engine.evaluate([], now=10.0)
        notifier.deliver.assert_called_once()
        self.assertEqual(engine.counters()['notifications']['delivered'], 1)

    def test_pending_recovery_does_not_notify(self):
        probe = _FakeProbe(on=True)
        notifier = MagicMock()
        engine = self._engine(
            notifier,
            Rule('Blip', 'warning', '测试规则', 10, probe),
        )
        engine.evaluate([], now=0.0)
        probe.on = False
        self.assertEqual(engine.evaluate([], now=1.0)[0]['state'], 'inactive')
        notifier.deliver.assert_not_called()

    def test_firing_recovery_sends_resolved(self):
        probe = _FakeProbe(on=True)
        notifier = MagicMock()
        notifier.deliver.return_value = {
            'delivered': 1, 'failed': 0, 'skipped': 0,
        }
        engine = self._engine(
            notifier,
            Rule('Recovered', 'critical', '测试规则', 0, probe),
        )
        engine.evaluate([], now=0.0)
        probe.on = False
        self.assertEqual(engine.evaluate([], now=5.0)[0]['state'], 'inactive')
        events = notifier.deliver.call_args.args[0]
        self.assertEqual([event['event'] for event in events], ['resolved'])
        self.assertEqual(events[0]['state'], 'inactive')
        self.assertEqual(events[0]['active_seconds'], 5.0)

    def test_multiple_transitions_are_delivered_as_one_batch(self):
        notifier = MagicMock()
        notifier.deliver.return_value = {
            'delivered': 2, 'failed': 0, 'skipped': 0,
        }
        engine = self._engine(
            notifier,
            Rule('First', 'warning', '规则一', 0, _FakeProbe(on=True)),
            Rule('Second', 'critical', '规则二', 0, _FakeProbe(on=True)),
        )
        engine.evaluate([], now=0.0)
        notifier.deliver.assert_called_once()
        events = notifier.deliver.call_args.args[0]
        self.assertEqual([event['name'] for event in events], ['First', 'Second'])
        self.assertEqual(engine.counters()['notifications']['delivered'], 2)

    def test_delivery_payload_headers_and_bearer_token(self):
        opener = MagicMock(return_value=self._response())
        notifier = AlertNotifier(
            url='https://alerts.example.test/hook',
            token='hook-secret',
            timeout=1.5,
            service_name='campus-test',
            opener=opener,
        )
        engine = self._engine(
            notifier,
            Rule('Payload', 'critical', '载荷测试', 0, _FakeProbe(on=True)),
        )
        engine.evaluate([], now=10.0)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, 'https://alerts.example.test/hook')
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.get_header('Content-type'),
                         'application/json; charset=utf-8')
        self.assertEqual(request.get_header('User-agent'),
                         'campus-glimmer-alerts/1')
        self.assertEqual(request.get_header('Authorization'), 'Bearer hook-secret')
        self.assertEqual(opener.call_args.kwargs['timeout'], 1.5)
        payload = json.loads(request.data.decode('utf-8'))
        self.assertEqual(payload['service'], 'campus-test')
        self.assertEqual(payload['generated_at'], 10.0)
        self.assertEqual(payload['events'][0]['event'], 'firing')
        self.assertEqual(payload['events'][0]['name'], 'Payload')
        self.assertEqual(payload['events'][0]['state'], 'firing')

    def test_unconfigured_webhook_skips_without_opening_network(self):
        opener = MagicMock()
        engine = self._engine(
            AlertNotifier(url='', opener=opener),
            Rule('QuietHook', 'warning', '测试规则', 0, _FakeProbe(on=True)),
        )
        engine.evaluate([], now=0.0)
        opener.assert_not_called()
        self.assertEqual(engine.counters()['notifications']['skipped'], 1)

    def test_delivery_failure_is_counted_and_does_not_escape(self):
        def timeout_opener(*_args, **_kwargs):
            raise TimeoutError('slow')

        def status_opener(*_args, **_kwargs):
            return self._response(503)

        for label, opener in (('timeout', timeout_opener),
                              ('non-2xx', status_opener)):
            with self.subTest(label=label):
                engine = self._engine(
                    AlertNotifier(url='https://alerts.example.test/hook',
                                  opener=opener),
                    Rule('FailingHook', 'warning', '测试规则', 0,
                         _FakeProbe(on=True)),
                )
                with self.assertLogs('campus_glimmer.alerts', level='WARNING'):
                    state = engine.evaluate([], now=0.0)[0]
                self.assertEqual(state['state'], 'firing')
                self.assertEqual(
                    engine.counters()['notifications']['failed'], 1,
                )

    def test_a_broken_notifier_cannot_take_the_metrics_endpoint_down(self):
        reset_registry()
        reset_engine()
        self.addCleanup(reset_registry)
        self.addCleanup(reset_engine)
        engine = get_engine()
        notifier = MagicMock()
        notifier.deliver.side_effect = RuntimeError('broken notifier')
        rule = Rule('BrokenHook', 'warning', '测试规则', 0, _FakeProbe(on=True))
        with patch.object(engine, '_rules', (rule,)), \
                patch.object(engine, '_notifier', notifier), \
                self.assertLogs('campus_glimmer.alerts', level='WARNING'):
            response = self.client.get(METRICS_PATH, **AUTH)
        self.assertEqual(response.status_code, 200)
        samples = parse_exposition(response.content.decode('utf-8'))
        self.assertEqual(
            find(samples, 'campus_glimmer_alert_notifications_total',
                 result='failed'),
            [1],
        )

    def test_notification_counts_are_exposed_in_prometheus(self):
        engine = self._engine(
            AlertNotifier(url=''),
            Rule('ExposedHook', 'warning', '测试规则', 0, _FakeProbe(on=True)),
        )
        engine.evaluate([], now=0.0)
        samples = parse_exposition(
            render_families(_alert_families(engine.states(), engine.counters()))
        )
        self.assertEqual(
            find(samples, 'campus_glimmer_alert_notifications_total',
                 result='skipped'),
            [1],
        )
        self.assertEqual(
            find(samples, 'campus_glimmer_alert_notifications_total',
                 result='failed'),
            [0],
        )





class AlertHistoryTests(SimpleTestCase):
    """有界历史只帮助排障，不把自己伪装成持久化审计或可靠投递。"""

    def _engine(self, for_seconds=0, on=True):
        self.probe = _FakeProbe(on=on)
        return AlertEngine(rules=[
            Rule('Probe', 'warning', '测试规则', for_seconds, self.probe),
        ])

    def test_history_starts_empty(self):
        engine = self._engine()
        self.assertEqual(engine.history(), [])
        self.assertEqual(
            engine.counters()['transitions'],
            {'firing': 0, 'resolved': 0},
        )

    def test_a_firing_transition_is_recorded_with_safe_fields(self):
        engine = self._engine()
        engine.evaluate([], now=10.0)
        history = engine.history()
        self.assertEqual(len(history), 1)
        event = history[0]
        self.assertEqual(event['event'], 'firing')
        self.assertEqual(event['name'], 'Probe')
        self.assertEqual(event['severity'], 'warning')
        self.assertEqual(event['state'], 'firing')
        self.assertEqual(event['summary'], '测试规则')
        self.assertEqual(event['value'], 1.0)
        self.assertEqual(event['detail'], '测试条件')
        self.assertEqual(event['since'], 10.0)
        self.assertEqual(event['occurred_at'], 10.0)
        self.assertEqual(event['active_seconds'], 0.0)
        self.assertEqual(
            set(event),
            {
                'event', 'name', 'severity', 'summary', 'state', 'value',
                'detail', 'since', 'occurred_at', 'active_seconds', 'instance',
            },
        )

    def test_ongoing_firing_is_not_recorded_again(self):
        engine = self._engine()
        engine.evaluate([], now=0.0)
        engine.evaluate([], now=10.0)
        self.assertEqual(len(engine.history()), 1)
        self.assertEqual(engine.counters()['transitions']['firing'], 1)

    def test_same_evaluation_uses_reverse_rule_order_for_newest_first(self):
        engine = AlertEngine(rules=[
            Rule('First', 'warning', '规则一', 0, _FakeProbe(on=True)),
            Rule('Second', 'critical', '规则二', 0, _FakeProbe(on=True)),
        ])
        engine.evaluate([], now=0.0)
        self.assertEqual(
            [event['name'] for event in engine.history()],
            ['Second', 'First'],
        )

    def test_resolved_is_recorded_after_firing_newest_first(self):
        engine = self._engine(on=True)
        engine.evaluate([], now=0.0)
        self.probe.on = False
        engine.evaluate([], now=5.0)
        history = engine.history()
        self.assertEqual(
            [event['event'] for event in history],
            ['resolved', 'firing'],
        )
        self.assertEqual(history[0]['state'], 'inactive')
        self.assertEqual(history[0]['since'], 0.0)
        self.assertEqual(history[0]['occurred_at'], 5.0)
        self.assertEqual(history[0]['active_seconds'], 5.0)

    def test_pending_recovery_leaves_no_transition_history(self):
        engine = self._engine(for_seconds=10, on=True)
        engine.evaluate([], now=0.0)
        self.probe.on = False
        engine.evaluate([], now=1.0)
        self.assertEqual(engine.history(), [])
        self.assertEqual(
            engine.counters()['transitions'],
            {'firing': 0, 'resolved': 0},
        )

    def test_history_keeps_only_the_latest_transitions(self):
        engine = self._engine()
        for step in range(ALERT_HISTORY_LIMIT + 1):
            self.probe.on = step % 2 == 0
            engine.evaluate([], now=float(step))
        history = engine.history()
        self.assertEqual(len(history), ALERT_HISTORY_LIMIT)
        self.assertEqual(history[0]['event'], 'firing')
        self.assertEqual(history[0]['occurred_at'], float(ALERT_HISTORY_LIMIT))
        self.assertEqual(history[-1]['event'], 'resolved')
        self.assertEqual(history[-1]['occurred_at'], 1.0)

    def test_history_is_handed_out_as_copies(self):
        engine = self._engine()
        engine.evaluate([], now=0.0)
        history = engine.history()
        history.append({'event': 'tampered'})
        history[0]['event'] = 'tampered'
        self.assertEqual(len(engine.history()), 1)
        self.assertEqual(engine.history()[0]['event'], 'firing')

    def test_transition_counts_match_history_and_are_exposed_by_event(self):
        engine = self._engine(on=True)
        engine.evaluate([], now=0.0)
        self.probe.on = False
        engine.evaluate([], now=4.0)
        self.assertEqual(
            engine.counters()['transitions'],
            {'firing': 1, 'resolved': 1},
        )
        self.assertEqual(
            sum(engine.counters()['transitions'].values()),
            len(engine.history()),
        )
        samples = parse_exposition(
            render_families(_alert_families(engine.states(), engine.counters()))
        )
        self.assertEqual(
            find(samples, 'campus_glimmer_alert_transitions_total',
                 event='firing'),
            [1],
        )
        self.assertEqual(
            find(samples, 'campus_glimmer_alert_transitions_total',
                 event='resolved'),
            [1],
        )


@override_settings(METRICS_TOKEN=TOKEN)
@override_settings(ALERT_HISTORY_DB_ENABLED=False)
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
                     'campus_glimmer_alert_notifications_total',
                     'campus_glimmer_alert_transitions_total',
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
        self.assertEqual(declared[-8:], [
            'campus_glimmer_alert_firing',
            'campus_glimmer_alert_pending',
            'campus_glimmer_alert_active_seconds',
            'campus_glimmer_alert_notifications_total',
            'campus_glimmer_alert_transitions_total',
            'campus_glimmer_alert_history_errors_total',
            'campus_glimmer_alert_evaluations_total',
            'campus_glimmer_alert_evaluation_errors_total',
        ])


@override_settings(METRICS_TOKEN=TOKEN, ALERT_HISTORY_DB_ENABLED=False)
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

    def test_the_payload_exposes_a_bounded_process_history(self):
        engine = get_engine()
        rule = Rule('History', 'critical', '历史测试', 0, _FakeProbe(on=True))
        with patch.object(engine, '_rules', (rule,)):
            response = self.client.get(ALERTS_PATH, **AUTH)
        payload = json.loads(response.content.decode('utf-8'))
        self.assertEqual(payload['history'], engine.history())
        self.assertEqual(payload['history_limit'], ALERT_HISTORY_LIMIT)
        self.assertEqual(payload['history_scope'], 'process')
        self.assertIs(payload['history_persistent'], False)
        self.assertIs(payload['history_degraded'], False)
        self.assertIsNone(payload['history_retention'])
        self.assertEqual(payload['history'][0]['event'], 'firing')

    def test_the_payload_history_never_contains_webhook_credentials(self):
        engine = get_engine()
        notifier = MagicMock()
        notifier.url = 'https://alerts.example.test/hook?token=hook-secret'
        notifier.token = 'hook-secret'
        notifier.deliver.return_value = {
            'delivered': 1, 'failed': 0, 'skipped': 0,
        }
        rule = Rule('PrivateHook', 'warning', '凭据测试', 0,
                    _FakeProbe(on=True))
        with patch.object(engine, '_rules', (rule,)),                 patch.object(engine, '_notifier', notifier):
            response = self.client.get(ALERTS_PATH, **AUTH)
        body = response.content.decode('utf-8')
        self.assertNotIn('hook-secret', body)
        self.assertNotIn('https://alerts.example.test', body)
        self.assertEqual(engine.history()[0]['name'], 'PrivateHook')

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


@override_settings(METRICS_TOKEN=TOKEN, ALERT_HISTORY_DB_ENABLED=False)
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
class _BrokenAlertHistory:
    retention_limit = DB_HISTORY_LIMIT

    def record(self, _event):
        raise OperationalError('history database unavailable')

    def recent(self, _limit):
        raise OperationalError('history database unavailable')


@override_settings(METRICS_TOKEN=TOKEN)
class AlertHistoryPersistenceTests(TestCase):
    """持久化历史跨重启可查，但数据库故障不能成为告警链路的单点。"""

    def setUp(self):
        reset_engine()
        self.addCleanup(reset_engine)

    def _rule(self, name='Persistent'):
        return Rule(name, 'critical', '持久化测试', 0, _FakeProbe(on=True))

    def test_a_transition_survives_a_new_engine(self):
        first = AlertEngine(rules=[self._rule()], history_store=DatabaseAlertHistory())
        first.evaluate([], now=10.0)
        self.assertEqual(AlertTransition.objects.count(), 1)

        restarted = AlertEngine(rules=[], history_store=DatabaseAlertHistory())
        snapshot = restarted.history_snapshot()
        self.assertEqual(snapshot['scope'], 'database')
        self.assertIs(snapshot['persistent'], True)
        self.assertIs(snapshot['degraded'], False)
        self.assertEqual(snapshot['retention_limit'], DB_HISTORY_LIMIT)
        self.assertEqual(snapshot['events'][0]['event'], 'firing')
        self.assertEqual(snapshot['events'][0]['name'], 'Persistent')
        self.assertEqual(snapshot['events'][0]['occurred_at'], 10.0)
        self.assertTrue(snapshot['events'][0]['instance'])

    def test_firing_and_resolution_are_both_persisted_newest_first(self):
        probe = _FakeProbe(on=True)
        engine = AlertEngine(
            rules=[Rule('Cycle', 'warning', '周期测试', 0, probe)],
            history_store=DatabaseAlertHistory(),
        )
        engine.evaluate([], now=1.0)
        probe.on = False
        engine.evaluate([], now=7.0)
        history = engine.history()
        self.assertEqual([item['event'] for item in history], ['resolved', 'firing'])
        self.assertEqual(AlertTransition.objects.count(), 2)
        self.assertEqual(history[0]['active_seconds'], 6.0)

    def test_database_history_is_bounded(self):
        with patch('observability.history.DB_HISTORY_LIMIT', 2):
            store = DatabaseAlertHistory()
            probe = _FakeProbe(on=True)
            engine = AlertEngine(
                rules=[Rule('Bounded', 'warning', '有界测试', 0, probe)],
                history_store=store,
            )
            for step in range(4):
                probe.on = step % 2 == 0
                engine.evaluate([], now=float(step))
            self.assertEqual(AlertTransition.objects.count(), 2)

    def test_write_and_read_failures_fall_back_to_memory(self):
        engine = AlertEngine(
            rules=[self._rule('Degraded')],
            history_store=_BrokenAlertHistory(),
        )
        states = engine.evaluate([], now=3.0)
        self.assertEqual(states[0]['state'], 'firing')
        snapshot = engine.history_snapshot()
        self.assertEqual(snapshot['scope'], 'process')
        self.assertIs(snapshot['persistent'], False)
        self.assertIs(snapshot['degraded'], True)
        self.assertEqual(snapshot['events'][0]['name'], 'Degraded')
        self.assertEqual(
            engine.counters()['history_errors'],
            {'write': 1, 'read': 1},
        )

    def test_the_alerts_endpoint_uses_database_history_by_default(self):
        engine = get_engine()
        with patch.object(engine, '_rules', (self._rule('EndpointPersistent'),)):
            response = self.client.get(ALERTS_PATH, **AUTH)
        payload = json.loads(response.content.decode('utf-8'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload['history_scope'], 'database')
        self.assertIs(payload['history_persistent'], True)
        self.assertIs(payload['history_degraded'], False)
        self.assertEqual(payload['history_retention'], DB_HISTORY_LIMIT)
        self.assertEqual(payload['history'][0]['name'], 'EndpointPersistent')

    def test_database_failure_cannot_take_down_either_observability_endpoint(self):
        engine = get_engine()
        with patch.object(engine, '_history_store', _BrokenAlertHistory()), \
                patch.object(engine, '_rules', (self._rule('OutageSafe'),)):
            metrics = self.client.get(METRICS_PATH, **AUTH)
            alerts = self.client.get(ALERTS_PATH, **AUTH)
        self.assertEqual(metrics.status_code, 200)
        self.assertEqual(alerts.status_code, 200)
        payload = json.loads(alerts.content.decode('utf-8'))
        self.assertEqual(payload['history_scope'], 'process')
        self.assertIs(payload['history_persistent'], False)
        self.assertIs(payload['history_degraded'], True)
        self.assertEqual(payload['history'][0]['name'], 'OutageSafe')
        samples = parse_exposition(metrics.content.decode('utf-8'))
        self.assertEqual(
            find(samples, 'campus_glimmer_alert_history_errors_total',
                 operation='write'),
            [1],
        )

    @override_settings(ALERT_HISTORY_DB_ENABLED=False)
    def test_persistence_can_be_disabled_without_changing_the_engine_contract(self):
        engine = reset_engine()
        self.assertIsNone(engine._history_store)
        with patch.object(engine, '_rules', (self._rule('MemoryOnly'),)):
            engine.evaluate([], now=1.0)
        self.assertEqual(AlertTransition.objects.count(), 0)
        self.assertEqual(engine.history_snapshot()['scope'], 'process')
