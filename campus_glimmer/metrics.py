# campus_glimmer/metrics.py
"""进程级 HTTP 指标，以及 Prometheus 文本格式的暴露端点。

为什么自己写而不引 client 库：真正需要的只有一个计数器、一个直方图和一段
文本序列化，引一个库换来的是一份新的升级面和一堆用不上的导出器。需要小心
的地方也不在序列化，而在标签基数——拿 ``request.path`` 当标签值的话，
``/listings/1/``、``/listings/2/`` 每条都是一个新序列，一天就能把 Prometheus
的内存和每次抓取的体积一起打爆。所以这里只写 URL 模板，并给序列总数封顶。

端点默认是关的：只有设置了 ``DJANGO_METRICS_TOKEN`` 才会响应，否则返回 404。
监控端点对外暴露的是内部拓扑（有哪些路由、大致多快），不该默认可见。

多副本下的口径：计数器只在本进程内累计，这是 Prometheus 多目标模型的默认
形态；但本项目是单机小规模部署，抓取目标通常只有一个地址，只抓一个副本等于
把另外几个副本的流量读成不存在。所以配置了跨进程缓存（生产就是 Redis）时，
每个 worker 周期性地把整份快照写进共享存储，``/metrics/`` 读回全部实例的
最近快照合并成一份舰队视图。共享存储读不动时退回到本进程视图，并把失败次数
记进指标；禁用共享时也有一个序列明说「你现在看到的是一个进程，不是整个
服务」。启用共享后请只把一个抓取目标指向 ``/metrics/``：每个副本返回的都是
同一份合并视图，重复抓取会把总量按副本数翻倍。
"""

import hmac
import logging
import os
import socket
import threading
import time

from django.conf import settings
from django.core.cache import caches
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)

# 与 urls.py 共用一个常量，避免「视图在 /metrics/、中间件却在跳过另一个路径」
# 这种只会在生产里显形的漂移。
METRICS_PATH = '/metrics/'
METRICS_URL_NAME = 'metrics'

PREFIX = 'campus_glimmer'

# 延迟分桶（秒）。取值的依据是这条站真实的耗时分布：本地 SQLite 上普通页面
# 在几十毫秒，图片上传和运营看板在几百毫秒到数秒。中间段密、两端疏，才能把
# p95 和「是不是整体变慢了」这两件事切开。
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# 每个指标族最多保留多少组标签值。超出之后新组合并进 overflow 序列：基数失控
# 的后果是内存和抓取体积同时失控，必须硬封顶；而封顶本身又是一次故障，所以
# 它要在暴露结果里可见（campus_glimmer_metrics_label_overflow）。
MAX_SERIES = 500

# URL 解析失败的请求（404、被前置中间件短路的请求）没有路由模板可写，
# 统一落到这个值。想知道是哪个路径 404 的，去日志里按关联 ID 查——那是
# 日志的活，不是指标的活。
UNMATCHED_ROUTE = 'unmatched'
OVERFLOW_ROUTE = 'overflow'

# 单个标签值的长度上限。URL 模板由我们自己的 urlconf 生成，本来不该有长值，
# 截断只是为了让「万一」不变成一次 OOM。
LABEL_VALUE_LIMIT = 200

# ---- 跨副本共享：缓存键与节奏 ----

# 快照与实例名单在缓存里的键。默认缓存自带 KEY_PREFIX，这里再写一层是为了
# 直接用 redis-cli 看的时候自己能看懂，不是重复加前缀。
SHARED_SNAPSHOT_KEY = 'metrics:fleet:instance:%s'
SHARED_INDEX_KEY = 'metrics:fleet:instances'

# 发布间隔与快照存活时间。TTL 取间隔的六倍：连续六次发布失败才让一个实例
# 从舰队视图里消失，一次 GC 停顿或重启不该让视图抖一下。
DEFAULT_SHARE_INTERVAL = 5.0
DEFAULT_SHARE_TTL = 30.0


def _sanitize_label(value):
    text = str(value or '')
    if len(text) > LABEL_VALUE_LIMIT:
        text = text[:LABEL_VALUE_LIMIT]
    return text


def _status_class(status):
    """把状态码压成 2xx/3xx/4xx/5xx。

    按精确状态码分组等于把基数再乘一个量级，而运维真正要看的是「5xx 率抬头」
    这种形状，不是 502 和 503 哪个更多。
    """

    if 100 <= status < 600:
        return '%dxx' % (status // 100)
    return 'other'


def _format_number(value):
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return repr(number)


# Prometheus 的标签值里反斜杠、双引号和换行必须转义。换行尤其致命：它会把
# 一行指标掰成两行，抓取端直接报解析错误，整套监控一起瞎掉。
_LABEL_ESCAPES = {'\\': '\\\\', '"': '\\"', '\n': '\\n', '\r': '\\r'}


def _escape_label(value):
    return ''.join(_LABEL_ESCAPES.get(char, char) for char in str(value))


class MetricsRegistry:
    """线程安全的计数器与直方图容器。

    gunicorn 默认是多线程 worker，所有读写都必须过同一把锁。锁内只做最小
    的字典操作，序列化在锁外进行——抓取本身不该成为业务请求的竞争对手。
    """

    def __init__(self, buckets=LATENCY_BUCKETS, max_series=MAX_SERIES):
        self._buckets = tuple(float(upper) for upper in buckets)
        self._max_series = max_series
        self._lock = threading.Lock()
        self._requests = {}
        self._durations = {}
        self._in_flight = 0
        self._started_at = time.time()
        self._overflowed = False
        self._shared_read_errors = 0

    # ---- 采集侧 ----

    def enter_request(self):
        with self._lock:
            self._in_flight += 1

    def leave_request(self, method, route, status, duration):
        method = _sanitize_label(method) or 'UNKNOWN'
        route = _sanitize_label(route) or UNMATCHED_ROUTE
        status_class = _status_class(status)
        with self._lock:
            if self._in_flight > 0:
                self._in_flight -= 1

            counter_key = self._bounded(self._requests, (route, method, status_class))
            self._requests[counter_key] = self._requests.get(counter_key, 0) + 1

            duration_key = self._bounded(self._durations, (route, method))
            slot = self._durations.get(duration_key)
            if slot is None:
                # [count, sum, 每桶累计数……]
                slot = [0, 0.0] + [0] * len(self._buckets)
                self._durations[duration_key] = slot
            slot[0] += 1
            slot[1] += duration
            for index, upper in enumerate(self._buckets):
                if duration <= upper:
                    slot[2 + index] += 1

    def note_shared_read_error(self):
        """记一次共享存储读取失败。

        监控自己的故障也必须可见：一次静默的降级会让「舰队视图」悄悄变回
        「一个副本的视图」，而看板上没有任何东西提醒过这件事。
        """

        with self._lock:
            self._shared_read_errors += 1

    def snapshot(self):
        """把当前状态取成一份可序列化的快照。

        发布线程把它写进共享存储，抓取端点拿它去和别处的快照合并。用嵌套
        列表而不是元组字典，是因为缓存后端只认可序列化对象，而 JSON 没有
        元组、字典的键也必须是字符串。
        """

        with self._lock:
            return {
                'instance': instance_id(),
                'published_at': time.time(),
                'requests': [
                    [route, method, status_class, count]
                    for (route, method, status_class), count in self._requests.items()
                ],
                'durations': [
                    [route, method] + list(slot)
                    for (route, method), slot in self._durations.items()
                ],
                'in_flight': self._in_flight,
                'overflowed': self._overflowed,
                'started_at': self._started_at,
                'buckets': list(self._buckets),
                'build': {
                    'service': settings.METRICS_SERVICE_NAME,
                    'python': _python_version(),
                    'django': _django_version(),
                },
            }

    # ---- 暴露侧 ----

    def render(self, view=None):
        """把一份视图序列化成 Prometheus 文本。

        ``view`` 缺省时是本进程快照；传进来的也可以是合并后的舰队视图
        （见 :func:`merge_fleet`）。序列化只读传入的这份数据，同一份视图
        重复渲染得到同一段文本。
        """

        if view is None:
            view = self.snapshot()
        with self._lock:
            shared_read_errors = self._shared_read_errors

        requests = sorted(
            (tuple(item[:3]), item[3]) for item in view['requests']
        )
        duration_items = sorted(
            ((item[0], item[1]), list(item[2:])) for item in view['durations']
        )
        in_flight = view['in_flight']
        started_at = view['started_at']
        overflowed = view['overflowed']
        buckets = list(view['buckets'])
        meta = view.get('meta') or {}
        builds = view.get('builds') or [_build_key(view['build'])]

        lines = []

        lines.extend(_header(
            PREFIX + '_http_requests_total', 'counter',
            'HTTP 请求总数，按请求方法、路由模板和状态类别聚合。',
        ))
        for (route, method, status_class), value in requests:
            lines.append(_sample(
                PREFIX + '_http_requests_total',
                (('method', method), ('route', route), ('status_class', status_class)),
                value,
            ))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_http_request_duration_seconds', 'histogram',
            'HTTP 请求耗时直方图（秒），只按路由模板聚合。',
        ))
        for (route, method), slot in duration_items:
            labels = (('method', method), ('route', route))
            for index, upper in enumerate(buckets):
                lines.append(_sample(
                    PREFIX + '_http_request_duration_seconds_bucket',
                    labels + (('le', _format_number(upper)),),
                    slot[2 + index],
                ))
            # Prometheus 要求直方图必须带 +Inf 桶，否则分位数算不出来。
            lines.append(_sample(
                PREFIX + '_http_request_duration_seconds_bucket',
                labels + (('le', '+Inf'),),
                slot[0],
            ))
            lines.append(_sample(
                PREFIX + '_http_request_duration_seconds_sum', labels, slot[1],
            ))
            lines.append(_sample(
                PREFIX + '_http_request_duration_seconds_count', labels, slot[0],
            ))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_http_requests_in_flight', 'gauge',
            '正在处理中的请求数。',
        ))
        lines.append(_sample(PREFIX + '_http_requests_in_flight', (), in_flight))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_metrics_label_overflow', 'gauge',
            '标签组合触及序列上限时为 1，此时部分请求被并入 overflow 路由。',
        ))
        lines.append(_sample(
            PREFIX + '_metrics_label_overflow', (), 1 if overflowed else 0,
        ))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_process_start_time_seconds', 'gauge',
            '进程启动时间（Unix 时间戳）；共享聚合开启时是视图内最早启动的实例。',
        ))
        lines.append(_sample(PREFIX + '_process_start_time_seconds', (), started_at))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_build_info', 'gauge',
            '构建信息，值恒为 1，用于在指标里挂版本标签。',
        ))
        for build in builds:
            lines.append(_sample(PREFIX + '_build_info', build, 1))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_metrics_shared_enabled', 'gauge',
            '是否启用了跨副本共享聚合；为 0 时上面全部是本进程视角，不是整个服务的数字。',
        ))
        lines.append(_sample(
            PREFIX + '_metrics_shared_enabled', (), 1 if meta.get('shared') else 0,
        ))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_metrics_instances', 'gauge',
            '本次视图包含的实例数（含本进程）。',
        ))
        lines.append(_sample(
            PREFIX + '_metrics_instances', (), int(meta.get('instances', 1)),
        ))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_metrics_view_age_seconds', 'gauge',
            '视图内最旧的远端快照年龄（秒）；没有远端实例时为 0。',
        ))
        lines.append(_sample(
            PREFIX + '_metrics_view_age_seconds', (), float(meta.get('view_age', 0.0)),
        ))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_metrics_bucket_mismatch', 'gauge',
            '因直方图分桶布局与本地不同而未并入直方图的实例数；它们的请求计数仍然并入总数。',
        ))
        lines.append(_sample(
            PREFIX + '_metrics_bucket_mismatch', (),
            int(meta.get('bucket_mismatch', 0)),
        ))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_metrics_shared_read_errors_total', 'counter',
            '读取共享存储失败的次数；失败时该次抓取退化为本进程视图。',
        ))
        lines.append(_sample(
            PREFIX + '_metrics_shared_read_errors_total', (), shared_read_errors,
        ))

        return '\n'.join(lines) + '\n'

    def _bounded(self, store, key):
        """取标签组合的存储键；达到序列上限后折叠到 overflow 路由。"""

        if key in store or len(store) < self._max_series:
            return key
        self._overflowed = True
        return (OVERFLOW_ROUTE,) + key[1:]


_registry = None
_registry_lock = threading.Lock()


def get_registry():
    """进程内唯一的指标容器。"""

    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = MetricsRegistry()
    return _registry


def reset_registry():
    """换一个新的空容器。测试之间需要干净起点时才用。"""

    global _registry
    with _registry_lock:
        _registry = MetricsRegistry()
    return _registry


def _header(name, kind, description):
    return (
        '# HELP %s %s' % (name, description),
        '# TYPE %s %s' % (name, kind),
    )


def _sample(name, labels, value):
    if labels:
        rendered = ','.join(
            '%s="%s"' % (key, _escape_label(label_value))
            for key, label_value in labels
        )
        return '%s{%s} %s' % (name, rendered, _format_number(value))
    return '%s %s' % (name, _format_number(value))


def _python_version():
    import sys
    return '.'.join(str(part) for part in sys.version_info[:3])


def _django_version():
    import django
    return django.get_version()


def _route_label(request):
    match = getattr(request, 'resolver_match', None)
    if match is None:
        # URL 解析失败，或者请求被前置中间件短路：没有路由模板可写。
        return UNMATCHED_ROUTE
    # 根路径的 route 是空串。空着不补的话，访问量最大的页面会和 404 混进
    # 同一个序列里，看板上「unmatched 有量」从此分不清是首页还是打错了路径。
    return _sanitize_label(getattr(match, 'route', '') or '/')


def _is_metrics_request(request):
    return request.path_info == METRICS_PATH


class MetricsMiddleware:
    """给每个请求计一次数、记一次耗时。

    路由标签在 ``finally`` 里才取：Django 的 URL 解析发生在
    ``BaseHandler._get_response`` 里，而整个 ``_get_response`` 又被中间件链
    包着，所以进中间件时 ``request.resolver_match`` 还是空的，出来才有。
    顺序反了的话所有请求都会落到 unmatched，指标等于白采。
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # 每个 worker 进程一个发布线程就够；禁用了共享存储时这里是空操作。
        # 放在 __init__ 而不是模块导入时：管理命令不该被一个用不上的线程跟着。
        ensure_publisher()

    def __call__(self, request):
        if _is_metrics_request(request):
            # 抓取自身不进指标，否则 Prometheus 的每次 scrape 都会把请求量
            # 和耗时曲线抬高一点，看板上的数字从此不完全是自己业务的。
            return self.get_response(request)

        registry = get_registry()
        method = _sanitize_label(request.method) or 'UNKNOWN'
        registry.enter_request()
        started = time.perf_counter()
        status = 500
        try:
            response = self.get_response(request)
            status = response.status_code
            return response
        finally:
            registry.leave_request(
                method, _route_label(request), status, time.perf_counter() - started,
            )


def _authorised(request):
    expected = (getattr(settings, 'METRICS_TOKEN', '') or '').strip()
    if not expected:
        return False
    supplied = request.headers.get('Authorization', '')
    scheme, _, value = supplied.partition(' ')
    if scheme.lower() != 'bearer':
        return False
    # 两边都编码成 bytes：compare_digest 只接受同为 str（且仅 ASCII）或
    # 同为 bytes，直接传客户端来的字符串会在遇到非 ASCII 时抛 TypeError。
    return hmac.compare_digest(value.strip().encode('utf-8'), expected.encode('utf-8'))


@require_http_methods(['GET', 'HEAD'])
def metrics_view(request):
    """以 Prometheus 文本格式暴露指标。"""

    if not _authorised(request):
        # 没配 token 或 token 不对时返回 404 而不是 403：这个端点的存在本身
        # 就不该被探测到。
        raise Http404('指标端点未启用')
    response = HttpResponse(
        render_fleet(),
        content_type='text/plain; version=0.0.4; charset=utf-8',
    )
    response.headers['Cache-Control'] = 'no-store'
    return response


# ---- 跨副本共享：开关、快照、存储、发布与合并 ----

def instance_id():
    """本进程在舰队里的名字：主机名加 PID。

    只做存储键，不进标签——标签基数是要钱的东西，而「哪个副本」这件事在
    单目标抓取的部署里没有读者。带上 PID 是因为一个容器里 gunicorn 可能
    跑多个 worker，它们各是一份计数器，同名会互相覆盖。
    """

    return '%s:%d' % (socket.gethostname(), os.getpid())


def shared_store_enabled():
    """这次部署要不要把指标共享出去。

    显式设置优先；没设置时按缓存后端判断：LocMem 是进程内的，多 worker 下
    各存各的，开了只会给出「只有我自己」的假象，所以默认关。Redis 一类
    跨进程后端默认开——那正是多副本部署本来就要用的东西。
    """

    override = getattr(settings, 'METRICS_SHARED', None)
    if override is not None:
        return bool(override)
    backend = (
        getattr(settings, 'CACHES', {}).get('default', {}).get('BACKEND', '') or ''
    ).lower()
    return 'locmem' not in backend


def _build_key(build):
    return tuple(sorted((key, value) for key, value in (build or {}).items()))


def merge_fleet(local, remotes, now=None):
    """把本进程快照和别处的快照合并成一份舰队视图。

    合并规则每一条都有理由，改之前先读设计文档：

    * 计数器按标签组合相加——这是「总共发生了多少次」的唯一算法；
    * 直方图只合并分桶布局相同的实例——混桶会算出从未发生过的分位数，
      比不合并更糟，所以不同的那几份只进计数器，并让
      ``campus_glimmer_metrics_bucket_mismatch`` 把它数出来；
    * 在途请求求和——每个副本此刻各自的数量，加起来才是服务此刻的数量；
    * 溢出标记取并——任一副本触顶，整个视图的分桶就已经不全了；
    * 启动时间取最早——「这批实例是什么时候起来的」问的是最早那个；
    * 构建信息去重——滚动发布期间新老版本同时在线，两份都是事实。
    """

    now = time.time() if now is None else now
    buckets = list(local['buckets'])
    requests = {}
    durations = {}

    def add_requests(items):
        for route, method, status_class, count in items:
            key = (route, method, status_class)
            requests[key] = requests.get(key, 0) + count

    def add_durations(items):
        for route, method, count, total, *counts in items:
            key = (route, method)
            slot = durations.get(key)
            if slot is None:
                slot = [0, 0.0] + [0] * len(counts)
                durations[key] = slot
            slot[0] += count
            slot[1] += total
            for index, value in enumerate(counts):
                slot[2 + index] += value

    add_requests(local['requests'])
    add_durations(local['durations'])

    in_flight = local['in_flight']
    overflowed = bool(local['overflowed'])
    started_at = local['started_at']
    builds = {_build_key(local['build'])}
    view_age = 0.0
    bucket_mismatch = 0

    for snapshot in remotes:
        view_age = max(
            view_age, max(0.0, now - float(snapshot.get('published_at', now))),
        )
        add_requests(snapshot['requests'])
        in_flight += snapshot['in_flight']
        overflowed = overflowed or bool(snapshot['overflowed'])
        started_at = min(started_at, snapshot['started_at'])
        builds.add(_build_key(snapshot['build']))
        if list(snapshot['buckets']) == buckets:
            add_durations(snapshot['durations'])
        else:
            bucket_mismatch += 1

    return {
        'requests': [
            [route, method, status_class, count]
            for (route, method, status_class), count in requests.items()
        ],
        'durations': [
            [route, method] + slot for (route, method), slot in durations.items()
        ],
        'in_flight': in_flight,
        'overflowed': overflowed,
        'started_at': started_at,
        'buckets': buckets,
        'build': local['build'],
        'builds': sorted(builds),
        'meta': {
            'shared': True,
            'instances': 1 + len(remotes),
            'view_age': view_age,
            'bucket_mismatch': bucket_mismatch,
        },
    }


class SharedMetricsStore:
    """把快照存进默认缓存，并在抓取时读回全部实例。

    为什么不引消息队列或专职聚合服务：需要共享的只有一份几百行的快照，
    而 Redis 已经是生产的硬依赖。索引用一张名单而不是 keys 扫描，是因为
    Django 缓存 API 没有可移植的 scan；名单是尽力而为的——两个副本同时
    发布可能互相覆盖，最坏情况是某个实例在下一个发布周期重新出现，而
    快照本身带 TTL，不会留下名字还在、数据没了的鬼数据。
    """

    def __init__(self, cache=None, ttl=None):
        self._cache = caches['default'] if cache is None else cache
        self._ttl = ttl if ttl is not None else _share_ttl()

    def publish(self, snapshot):
        instance = snapshot['instance']
        self._cache.set(SHARED_SNAPSHOT_KEY % instance, snapshot, self._ttl)
        index = list(self._cache.get(SHARED_INDEX_KEY) or [])
        if instance not in index:
            index.append(instance)
        self._cache.set(SHARED_INDEX_KEY, index, self._ttl)

    def read_instances(self, exclude=None):
        """读回除 ``exclude`` 之外的全部快照，顺手把过期的名字清掉。"""

        index = list(self._cache.get(SHARED_INDEX_KEY) or [])
        alive = []
        snapshots = []
        for instance in index:
            if instance == exclude:
                alive.append(instance)
                continue
            snapshot = self._cache.get(SHARED_SNAPSHOT_KEY % instance)
            if snapshot is None:
                # TTL 到了：这个实例已经不在舰队里，名字也不该留着。
                continue
            alive.append(instance)
            snapshots.append(snapshot)
        if alive != index:
            self._cache.set(SHARED_INDEX_KEY, alive, self._ttl)
        return snapshots


class SharedMetricsPublisher(threading.Thread):
    """周期性把本进程快照写进共享存储。

    为什么要一个线程而不是「被抓到时顺手发布」：抓取目标通常只有一个
    地址，另外几个副本永远等不到被抓的那一刻，它们的计数就永远不进舰队
    视图。周期发布让「进不进视图」只取决于自己活没活，不取决于负载均衡
    今天把请求转给谁。
    """

    def __init__(self, store=None, interval=None, registry_provider=get_registry):
        super().__init__(name='campus-glimmer-metrics-publisher', daemon=True)
        self._store = SharedMetricsStore() if store is None else store
        self._interval = interval if interval is not None else _share_interval()
        self._registry_provider = registry_provider
        self._stopped = threading.Event()
        self._last_publish_ok = True

    def run(self):
        while not self._stopped.wait(self._interval):
            self.publish_safely()

    def publish_safely(self):
        # 单拎出来是为了能离开线程单测：日志的翻转语义是「只在状态变化时记」，
        # 在真实线程里等五次发布才能观测到，既慢又抖。
        try:
            self.publish_once()
        except Exception as error:
            # 只在状态翻转时记日志：共享存储挂掉时每五秒一行同样的
            # 警告只会把真正要看的东西冲掉。
            if self._last_publish_ok:
                logger.warning(
                    '指标快照发布失败，舰队视图将停在旧快照：%s: %s',
                    type(error).__name__, error,
                )
            self._last_publish_ok = False
        else:
            if not self._last_publish_ok:
                logger.info('指标快照发布恢复')
            self._last_publish_ok = True

    def publish_once(self):
        self._store.publish(self._registry_provider().snapshot())

    def stop(self):
        self._stopped.set()


_publisher = None
_publisher_lock = threading.Lock()


def ensure_publisher():
    """确保本进程只有一个发布线程；禁用了共享存储时返回 ``None``。"""

    global _publisher
    if not shared_store_enabled():
        return None
    with _publisher_lock:
        if _publisher is None or not _publisher.is_alive():
            _publisher = SharedMetricsPublisher()
            _publisher.start()
        return _publisher


def stop_publisher():
    """停掉发布线程。测试里用来把线程的生命周期收回自己手里。"""

    global _publisher
    with _publisher_lock:
        publisher, _publisher = _publisher, None
    if publisher is not None:
        publisher.stop()


def _share_interval():
    try:
        interval = float(getattr(settings, 'METRICS_SHARE_INTERVAL', DEFAULT_SHARE_INTERVAL))
    except (TypeError, ValueError):
        interval = DEFAULT_SHARE_INTERVAL
    # 下限存在的意义是防呆：间隔为 0 会把发布线程变成忙循环。
    return max(interval, 0.1)


def _share_ttl():
    try:
        ttl = float(getattr(settings, 'METRICS_SHARE_TTL', DEFAULT_SHARE_TTL))
    except (TypeError, ValueError):
        ttl = DEFAULT_SHARE_TTL
    return max(ttl, _share_interval() + 1.0)


def render_fleet(registry=None, store=None, now=None):
    """渲染舰队视图：本进程当前状态加上共享存储里其他实例的最近快照。

    禁用共享、或者共享存储读不动时都退回到本进程视图——后者会把失败次数
    记进指标。退化的前提是宁可少算也要能抓到：监控端点自己 500 比数字旧
    一点更难排查。
    """

    registry = get_registry() if registry is None else registry
    local = registry.snapshot()
    if not shared_store_enabled():
        return registry.render(local)
    try:
        store = SharedMetricsStore() if store is None else store
        remotes = store.read_instances(exclude=local['instance'])
        # 合并也放进 try：别处是旧版本、或缓存里被写坏了格式时，这该算一次
        # 读失败，而不是让监控端点自己 500——抓不动的端点比数字旧一点的端点
        # 更难排查。
        view = merge_fleet(local, remotes, now=now)
    except Exception as error:
        registry.note_shared_read_error()
        logger.warning(
            '读取指标共享存储失败，本次抓取退化为本进程视图：%s: %s',
            type(error).__name__, error,
        )
        return registry.render(local)
    return registry.render(view)
