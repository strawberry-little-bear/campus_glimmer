# campus_glimmer/test_metrics_fleet.py
"""跨副本指标聚合的契约测试。

``/metrics/`` 在单进程里只回答「这个进程忙不忙」。可单机多副本的部署里抓取目标
通常只有一个地址：只抓一个副本，等于把另外几个副本的流量读成不存在。这里钉住的
就是把各副本合成一份舰队视图的每一条边界：

* **开关**。共享载体是默认缓存（生产即 Redis），所以开关默认按缓存后端判断：
  LocMem 是进程内的，多 worker 下各存各的，开了只会给出「只有我自己」的假象。
  拼错的值、自相矛盾的发布节奏，都必须在启动时就炸，不能变成看板上一个解释不
  了的数字。
* **合并语义**。计数器相加、直方图只合并分桶布局相同的实例、在途求和、溢出取并、
  启动时间取最早、构建信息去重——每一条都有理由，改之前先读设计文档。
* **降级**。共享存储读不动、或者别处写坏了格式，都退回本进程视图并把失败次数
  记进指标。监控端点自己 500 比数字旧一点更难排查。
* **不自我污染**。合并时必须排除本进程的快照，否则同一份计数被算两遍。
"""

import json
import os
import socket
import time
from unittest.mock import patch

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.locmem import LocMemCache
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from . import metrics as metrics_module
from campus_glimmer import settings as project_settings
from .metrics import (
    LATENCY_BUCKETS,
    METRICS_PATH,
    MetricsRegistry,
    SharedMetricsPublisher,
    SharedMetricsStore,
    get_registry,
    instance_id,
    merge_fleet,
    render_fleet,
    reset_registry,
    shared_store_enabled,
    stop_publisher,
)
from .test_metrics import find, parse_exposition

SHARED_CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'campus-glimmer-metrics-fleet',
    },
}
TOKEN = 'top-secret-token'
AUTH = {'HTTP_AUTHORIZATION': 'Bearer ' + TOKEN}


def _snapshot(**overrides):
    """造一份快照：先按 ``requests`` 真实累计，再覆盖指定字段。

    直方图、计数器和时间戳都从真注册器里来，测的是合并逻辑而不是手搓的数据结构。
    """

    registry = MetricsRegistry()
    for method, route, status, duration in overrides.pop('requests', ()):
        registry.leave_request(method, route, status, duration)
    snapshot = registry.snapshot()
    snapshot.update(overrides)
    return snapshot


def _duration_slot(view, route):
    for item in view['durations']:
        if item[0] == route:
            return item
    raise AssertionError('视图里没有 ' + route + ' 的直方图')


def _counter(view, route, status_class):
    for item in view['requests']:
        if item[0] == route and item[2] == status_class:
            return item[3]
    return 0


def _type_lines(text, name):
    return [line for line in text.split('\n') if line.startswith('# TYPE ' + name + ' ')]


class BrokenCache:
    """一读就炸的缓存，用来验证降级路径真的走到了。"""

    def get(self, key, default=None, version=None):
        raise RuntimeError('缓存连不上')

    def set(self, key, value, timeout=None, version=None):
        raise RuntimeError('缓存连不上')


class ExplodingStore:
    """publish 就抛异常的存储，用来验证发布线程不会被自己的故障带走。"""

    def publish(self, snapshot):
        raise RuntimeError('存储炸了')

    def read_instances(self, exclude=None):
        return []


class FlakyStore:
    """前几次发布失败、之后恢复的存储，用来观测日志的状态翻转。"""

    def __init__(self, failures):
        self.failures = failures
        self.published = []

    def publish(self, snapshot):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError('存储抖动')
        self.published.append(snapshot)

    def read_instances(self, exclude=None):
        return []


class MetricsSharingSwitchTests(SimpleTestCase):
    """共享开关的三态与发布节奏的启动期校验。"""

    def test_a_process_local_cache_disables_sharing_by_default(self):
        with override_settings(CACHES=SHARED_CACHES):
            # LocMem 每个 worker 各一份，共享只会给出「只有我自己」的假象。
            self.assertFalse(shared_store_enabled())

    def test_a_cross_process_cache_enables_sharing_by_default(self):
        redis = {'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache'}}
        with override_settings(CACHES=redis):
            # Redis 正是多副本部署本来就要用的东西，没理由再要求显式开一遍。
            self.assertTrue(shared_store_enabled())

    def test_an_explicit_setting_beats_the_backend_guess(self):
        redis = {'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache'}}
        with override_settings(CACHES=redis, METRICS_SHARED=False):
            self.assertFalse(shared_store_enabled())
        with override_settings(CACHES=SHARED_CACHES, METRICS_SHARED=True):
            self.assertTrue(shared_store_enabled())

    def test_a_misspelled_switch_fails_the_boot(self):
        # 静默默认的话，DJANGO_METRICS_SHARED=flase 会被读成「关」，
        # 而排查的人以为共享从来就没开过。
        os.environ['DJANGO_METRICS_SHARED'] = 'flase'
        try:
            with self.assertRaises(ImproperlyConfigured):
                project_settings._env_tristate('DJANGO_METRICS_SHARED')
        finally:
            del os.environ['DJANGO_METRICS_SHARED']
        self.assertIsNone(project_settings._env_tristate('DJANGO_METRICS_SHARED'))

    def test_a_pacing_that_expires_live_instances_fails_the_boot(self):
        # TTL 小于等于间隔时，活着的实例也会在两次发布之间从视图里过期。
        for interval, ttl in ((5.0, 5.0), (5.0, 1.0), (0.0, 30.0)):
            with self.subTest(interval=interval, ttl=ttl):
                with self.assertRaises(ImproperlyConfigured):
                    project_settings._share_pacing(interval, ttl)


class MetricsSnapshotShapeTests(SimpleTestCase):
    """快照必须能进缓存、能认出来是谁、能跨进程解析。"""

    def setUp(self):
        reset_registry()
        self.addCleanup(reset_registry)

    def test_a_snapshot_carries_identity_payload_and_pacing(self):
        registry = MetricsRegistry()
        registry.leave_request('GET', 'a/', 200, 0.01)
        snapshot = registry.snapshot()

        self.assertEqual(
            sorted(snapshot),
            ['buckets', 'build', 'durations', 'in_flight', 'instance',
             'overflowed', 'published_at', 'requests', 'shared_read_errors',
             'started_at'],
        )
        # 降级次数必须随快照走：只报本进程的话，合并后的视图分不出「全网在
        # 降级」和「只有这一个副本在降级」，而这两件事的处置完全不同。
        self.assertEqual(snapshot['shared_read_errors'], 0)
        registry.note_shared_read_error()
        self.assertEqual(registry.snapshot()['shared_read_errors'], 1)
        self.assertEqual(snapshot['instance'], instance_id())
        self.assertAlmostEqual(snapshot['published_at'], time.time(), delta=5)
        self.assertEqual(snapshot['buckets'], list(LATENCY_BUCKETS))
        self.assertEqual(snapshot['build']['service'], settings.METRICS_SERVICE_NAME)

    def test_a_snapshot_survives_a_json_round_trip(self):
        # 缓存后端只认可序列化对象，而 JSON 没有元组、键也必须是字符串。
        # 快照里混进一个元组，locmem 能过、Redis 当场炸。
        snapshot = _snapshot(requests=[('GET', 'a/', 200, 0.01)])
        self.assertEqual(json.loads(json.dumps(snapshot)), snapshot)

    def test_instance_id_is_stable_and_separates_workers(self):
        first = instance_id()
        self.assertEqual(first, instance_id())
        self.assertEqual(first, '%s:%d' % (socket.gethostname(), os.getpid()))
        # 一个容器里 gunicorn 可能起多个 worker，同名会让两份计数互相覆盖。
        with patch('campus_glimmer.metrics.os.getpid', return_value=os.getpid() + 1):
            self.assertNotEqual(instance_id(), first)


class MetricsFleetMergeTests(SimpleTestCase):
    """合并规则：每条都对应一种会算错的情况。"""

    def setUp(self):
        reset_registry()
        self.addCleanup(reset_registry)

    def test_counters_are_summed_across_instances(self):
        local = _snapshot(requests=[('GET', 'a/', 200, 0.01)] * 2)
        remote = _snapshot(instance='remote:1', requests=[('GET', 'a/', 200, 0.01)] * 3)

        view = merge_fleet(local, [remote])

        self.assertEqual(_counter(view, 'a/', '2xx'), 5)

    def test_histograms_are_summed_when_the_bucket_layouts_match(self):
        local = _snapshot(requests=[('GET', 'a/', 200, 0.02)] * 2)
        remote = _snapshot(instance='remote:1', requests=[('GET', 'a/', 200, 0.20)] * 3)

        slot = _duration_slot(merge_fleet(local, [remote]), 'a/')

        self.assertEqual(slot[2], 5)
        self.assertAlmostEqual(slot[3], 2 * 0.02 + 3 * 0.20)

    def test_in_flight_is_summed(self):
        local = _snapshot(in_flight=2)
        remote = _snapshot(instance='remote:1', in_flight=3)

        self.assertEqual(merge_fleet(local, [remote])['in_flight'], 5)

    def test_the_overflow_flag_is_a_union(self):
        # 任一副本触顶，整个视图的分桶就已经不全了，不能因为本进程没触顶就报 0。
        remote = _snapshot(instance='remote:1', overflowed=True)
        self.assertTrue(merge_fleet(_snapshot(), [remote])['overflowed'])
        self.assertFalse(merge_fleet(_snapshot(), [_snapshot(instance='remote:1')])['overflowed'])

    def test_the_start_time_takes_the_earliest_instance(self):
        local = _snapshot(started_at=1000.0)
        remote = _snapshot(instance='remote:1', started_at=900.0)

        self.assertEqual(merge_fleet(local, [remote])['started_at'], 900.0)

    def test_build_info_is_deduplicated(self):
        # 滚动发布期间新老版本同时在线，两份都是事实；相同的那份只该出现一次。
        local = _snapshot()
        same = _snapshot(instance='remote:1')
        other = _snapshot(instance='remote:2', build={
            'service': 'campus-glimmer', 'python': '3.11.0', 'django': '5.1',
        })

        view = merge_fleet(local, [same, other])

        self.assertEqual(len(view['builds']), 2)
        samples = parse_exposition(MetricsRegistry().render(view))
        # 去重是按内容判的：内容完全相同的两份只该留下一行，
        # 不同的那份（版本不同）必须留着——滚动发布期间新老版本同时在线。
        self.assertEqual(len(find(samples, 'campus_glimmer_build_info')), 2)

    def test_a_bucket_layout_mismatch_keeps_counters_but_drops_the_histogram(self):
        # 混桶会算出从未发生过的分位数，比不合并更糟，所以那份直方图整个丢掉，
        # 只把计数器并进来，并让 bucket_mismatch 把它数出来。
        local = _snapshot(requests=[('GET', 'a/', 200, 0.02)])
        remote = _snapshot(
            instance='remote:1', buckets=[0.1, 1.0],
            requests=[('GET', 'a/', 200, 0.02)] * 4,
        )

        view = merge_fleet(local, [remote])

        self.assertEqual(view['meta']['bucket_mismatch'], 1)
        self.assertEqual(_counter(view, 'a/', '2xx'), 5)
        self.assertEqual(_duration_slot(view, 'a/')[2], 1)


@override_settings(METRICS_SHARED=True)
class MetricsFleetStoreTests(SimpleTestCase):
    """共享存储的发布、读回、清理与降级。"""

    def setUp(self):
        reset_registry()
        stop_publisher()
        self.addCleanup(reset_registry)
        self.addCleanup(stop_publisher)

    def _store(self, location):
        return SharedMetricsStore(cache=LocMemCache(location, {}), ttl=30)

    def test_publish_then_read_round_trips_the_snapshot(self):
        store = self._store('campus-glimmer-fleet-round-trip')
        snapshot = _snapshot(instance='remote-a:1', requests=[('GET', 'a/', 200, 0.01)])

        store.publish(snapshot)
        read_back = store.read_instances()

        self.assertEqual([item['instance'] for item in read_back], ['remote-a:1'])
        self.assertEqual(read_back[0]['requests'], snapshot['requests'])

    def test_read_instances_excludes_the_local_instance(self):
        # 不排除自己，同一份计数会被算两遍——这是整个合并里最容易犯的错。
        store = self._store('campus-glimmer-fleet-exclude')
        store.publish(_snapshot(instance='me:1'))
        store.publish(_snapshot(instance='other:1'))

        self.assertEqual(
            [item['instance'] for item in store.read_instances(exclude='me:1')], ['other:1'])

    def test_an_instance_whose_snapshot_expired_is_pruned_from_the_index(self):
        # 名单是尽力而为的：名字还在、数据没了的鬼条目必须被清掉，
        # 否则视图会一直带着一个早已消失的实例。
        cache = LocMemCache('campus-glimmer-fleet-prune', {})
        store = SharedMetricsStore(cache=cache, ttl=30)
        cache.set(metrics_module.SHARED_INDEX_KEY, ['ghost:1'], 30)

        self.assertEqual(store.read_instances(), [])
        self.assertEqual(cache.get(metrics_module.SHARED_INDEX_KEY), [])

    def test_a_broken_snapshot_degrades_to_the_local_view(self):
        # 别处是旧版本、或缓存里被写坏了格式：这该算一次读失败，
        # 而不是让监控端点自己 500。
        cache = LocMemCache('campus-glimmer-fleet-broken', {})
        cache.set(metrics_module.SHARED_INDEX_KEY, ['broken:1'], 30)
        cache.set(
            metrics_module.SHARED_SNAPSHOT_KEY % 'broken:1', {'instance': 'broken:1'}, 30)

        samples = parse_exposition(render_fleet(
            registry=MetricsRegistry(), store=SharedMetricsStore(cache=cache)))

        self.assertEqual(find(samples, 'campus_glimmer_metrics_instances'), [1])
        self.assertEqual(
            find(samples, 'campus_glimmer_metrics_shared_read_errors_total'), [1])

    def test_a_read_failure_degrades_and_is_counted(self):
        with self.assertLogs('campus_glimmer.metrics', level='WARNING') as logs:
            samples = parse_exposition(render_fleet(
                registry=MetricsRegistry(), store=SharedMetricsStore(cache=BrokenCache())))

        self.assertEqual(find(samples, 'campus_glimmer_metrics_instances'), [1])
        self.assertEqual(
            find(samples, 'campus_glimmer_metrics_shared_read_errors_total'), [1])
        self.assertTrue(any('退化为本进程视图' in line for line in logs.output))

    def test_the_ttl_never_drops_below_the_interval(self):
        with override_settings(METRICS_SHARE_INTERVAL=5.0, METRICS_SHARE_TTL=1.0):
            self.assertEqual(metrics_module._share_ttl(), 6.0)
        with override_settings(METRICS_SHARE_INTERVAL=0.0, METRICS_SHARE_TTL=30.0):
            self.assertEqual(metrics_module._share_interval(), 0.1)


class MetricsPublisherTests(SimpleTestCase):
    """后台发布线程：真的在写、能停、故障带不走它、日志不刷屏。"""

    def setUp(self):
        stop_publisher()
        self.addCleanup(stop_publisher)

    def test_publish_once_writes_a_readable_snapshot(self):
        store = SharedMetricsStore(
            cache=LocMemCache('campus-glimmer-fleet-publish', {}), ttl=30)

        SharedMetricsPublisher(store=store, interval=0.01).publish_once()

        snapshots = store.read_instances()
        self.assertEqual([item['instance'] for item in snapshots], [instance_id()])

    def test_the_publisher_thread_stops_on_demand(self):
        publisher = SharedMetricsPublisher(store=ExplodingStore(), interval=0.01)
        publisher.start()
        time.sleep(0.1)
        self.assertTrue(publisher.is_alive())

        publisher.stop()
        publisher.join(timeout=5)

        self.assertFalse(publisher.is_alive())

    def test_a_failing_store_does_not_kill_the_publisher(self):
        # 共享存储挂掉时线程必须继续活着：下次存储恢复它还得发布，
        # 否则舰队视图会一直停在死前那一刻。
        publisher = SharedMetricsPublisher(store=ExplodingStore(), interval=0.01)
        publisher.start()
        time.sleep(0.2)
        alive = publisher.is_alive()
        publisher.stop()
        publisher.join(timeout=5)

        self.assertTrue(alive)

    def test_the_publisher_logs_only_when_the_state_flips(self):
        # 一次抖动只该留下两行：失败时一行警告，恢复时一行提示。
        # 中间每个周期都安静——共享存储每五秒抖一次，每周期一行同样的警告
        # 会把真正要看的东西从日志里冲掉。
        publisher = SharedMetricsPublisher(store=FlakyStore(failures=1), interval=0.01)
        with self.assertLogs('campus_glimmer.metrics', level='INFO') as logs:
            publisher.publish_safely()
            publisher.publish_safely()
            publisher.publish_safely()

        warnings = [line for line in logs.output if 'WARNING' in line]
        self.assertEqual(len(warnings), 1, '连续失败不该每周期刷一行同样的警告')

        recoveries = [line for line in logs.output if '发布恢复' in line]
        self.assertEqual(len(recoveries), 1, '恢复必须记一次，否则没人知道它回来了')


@override_settings(
    METRICS_TOKEN=TOKEN, METRICS_SHARED=True, CACHES=SHARED_CACHES)
class MetricsFleetEndpointTests(SimpleTestCase):
    """端到端：抓一次 /metrics/ 得到的是整个舰队的合并视图。"""

    def setUp(self):
        reset_registry()
        stop_publisher()
        self.cache = caches['default']
        self.cache.clear()
        self.addCleanup(self.cache.clear)
        self.addCleanup(reset_registry)
        self.addCleanup(stop_publisher)

    def test_the_fleet_series_are_present_and_well_formed(self):
        now = time.time()
        store = SharedMetricsStore(cache=self.cache, ttl=30)
        store.publish(_snapshot(instance='remote-a:1', published_at=now - 3))

        text = render_fleet(registry=MetricsRegistry(), store=store, now=now)
        samples = parse_exposition(text)
        names = {name for name, _labels, _value in samples}

        for name in (
            'campus_glimmer_metrics_shared_enabled',
            'campus_glimmer_metrics_instances',
            'campus_glimmer_metrics_view_age_seconds',
            'campus_glimmer_metrics_bucket_mismatch',
            'campus_glimmer_metrics_shared_read_errors_total',
        ):
            self.assertIn(name, names)
            self.assertEqual(len(_type_lines(text, name)), 1, name + ' 的 TYPE 行不唯一')

        self.assertEqual(find(samples, 'campus_glimmer_metrics_shared_enabled'), [1])
        self.assertEqual(find(samples, 'campus_glimmer_metrics_instances'), [2])
        self.assertEqual(find(samples, 'campus_glimmer_metrics_bucket_mismatch'), [0])
        self.assertEqual(
            find(samples, 'campus_glimmer_metrics_shared_read_errors_total'), [0])
        self.assertEqual(
            find(samples, 'campus_glimmer_metrics_view_age_seconds'), [3.0])

    def test_the_endpoint_merges_two_replicas_end_to_end(self):
        store = SharedMetricsStore(cache=self.cache, ttl=30)
        store.publish(_snapshot(
            instance='remote-a:1',
            requests=[('GET', 'listings/', 200, 0.01), ('POST', 'chat/send/', 500, 0.2)],
        ))
        get_registry().leave_request('GET', 'home/', 200, 0.02)

        response = self.client.get(METRICS_PATH, **AUTH)

        self.assertEqual(response.status_code, 200)
        samples = parse_exposition(response.content.decode('utf-8'))
        self.assertEqual(find(samples, 'campus_glimmer_metrics_instances'), [2])
        self.assertEqual(find(samples, 'campus_glimmer_http_requests_total', route='home/'), [1])
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='listings/'), [1])
        self.assertEqual(
            find(samples, 'campus_glimmer_http_requests_total', route='chat/send/',
                 status_class='5xx'), [1])

    def test_a_scrape_still_does_not_count_itself(self):
        for _ in range(2):
            response = self.client.get(METRICS_PATH, **AUTH)

        samples = parse_exposition(response.content.decode('utf-8'))
        self.assertEqual(find(samples, 'campus_glimmer_http_requests_total'), [])

    def test_the_endpoint_still_requires_the_token(self):
        with override_settings(METRICS_TOKEN=''):
            self.assertEqual(self.client.get(METRICS_PATH).status_code, 404)


@override_settings(METRICS_TOKEN=TOKEN, METRICS_SHARED=False, CACHES=SHARED_CACHES)
class MetricsFleetDisabledEndpointTests(SimpleTestCase):
    """关掉共享时，端点必须明说「这是一个进程，不是整个服务」。"""

    def setUp(self):
        reset_registry()
        self.addCleanup(reset_registry)

    def test_the_endpoint_reports_a_single_instance(self):
        response = self.client.get(METRICS_PATH, **AUTH)

        samples = parse_exposition(response.content.decode('utf-8'))
        self.assertEqual(find(samples, 'campus_glimmer_metrics_shared_enabled'), [0])
        self.assertEqual(find(samples, 'campus_glimmer_metrics_instances'), [1])
        self.assertEqual(find(samples, 'campus_glimmer_metrics_view_age_seconds'), [0])
        self.assertEqual(find(samples, 'campus_glimmer_metrics_bucket_mismatch'), [0])
