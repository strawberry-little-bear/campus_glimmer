# campus_glimmer/metrics.py
"""进程级 HTTP 指标，以及 Prometheus 文本格式的暴露端点。

为什么自己写而不引 client 库：真正需要的只有一个计数器、一个直方图和一段
文本序列化，引一个库换来的是一份新的升级面和一堆用不上的导出器。需要小心
的地方也不在序列化，而在标签基数——拿 ``request.path`` 当标签值的话，
``/listings/1/``、``/listings/2/`` 每条都是一个新序列，一天就能把 Prometheus
的内存和每次抓取的体积一起打爆。所以这里只写 URL 模板，并给序列总数封顶。

端点默认是关的：只有设置了 ``DJANGO_METRICS_TOKEN`` 才会响应，否则返回 404。
监控端点对外暴露的是内部拓扑（有哪些路由、大致多快），不该默认可见。
"""

import hmac
import threading
import time

from django.conf import settings
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_http_methods

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

    # ---- 暴露侧 ----

    def render(self):
        with self._lock:
            requests = sorted(self._requests.items())
            durations = {key: list(value) for key, value in self._durations.items()}
            in_flight = self._in_flight
            started_at = self._started_at
            overflowed = self._overflowed
        duration_items = sorted(durations.items())

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
            for index, upper in enumerate(self._buckets):
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
            '进程启动时间（Unix 时间戳）。',
        ))
        lines.append(_sample(PREFIX + '_process_start_time_seconds', (), started_at))

        lines.append('')
        lines.extend(_header(
            PREFIX + '_build_info', 'gauge',
            '构建信息，值恒为 1，用于在指标里挂版本标签。',
        ))
        lines.append(_sample(
            PREFIX + '_build_info',
            (('service', settings.METRICS_SERVICE_NAME),
             ('python', _python_version()),
             ('django', _django_version())),
            1,
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
    """以 Prometheus 文本格式暴露进程级指标。"""

    if not _authorised(request):
        # 没配 token 或 token 不对时返回 404 而不是 403：这个端点的存在本身
        # 就不该被探测到。
        raise Http404('指标端点未启用')
    response = HttpResponse(
        get_registry().render(),
        content_type='text/plain; version=0.0.4; charset=utf-8',
    )
    response.headers['Cache-Control'] = 'no-store'
    return response
