# campus_glimmer/alerts.py
"""按指标的阈值告警：把「现在有没有越界」从人的眼睛搬到代码里。

只评估、只报告：不呼叫任何人、不发邮件、不改任何业务状态。产物是
``/alerts/`` 上的一份 JSON 和 ``/metrics/`` 末尾的几个
``campus_glimmer_alert_*`` 序列，由外层监控系统决定怎么喊人——本项目没有、
也不打算自己实现一套值班轮换。

为什么在抓取时评估，而不是再起一个定时线程：Prometheus 模型里规则的评估
节奏就是抓取节奏，再起一个线程只是多一条要维护的生命周期；两份节奏一漂移，
看板上的告警就说不清是哪一次评估得出的。代价是没人抓取时没有评估——但没人
抓取的时候，也没人在看这些数字。

阈值是模块常量而不是环境变量：改阈值等于改「什么算故障」的定义，那应该走
一次提交和一次测试，而不是某个下午在部署面板上改一个数字。

规则的输入是 :func:`campus_glimmer.metrics.iter_samples` 给的样本，也就是
``/metrics/`` 正要暴露出去的那一批。告警判断的依据和看板上看到的数因此不可
能对不上——这是本模块唯一比「算法正确」更要紧的不变式。
"""

import logging
import math
import threading
import time
from collections import deque

from django.conf import settings
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_http_methods

# 路径常量与鉴权助手都从 metrics  import：观测端点只有一份鉴权实现，
# 抄第二份意味着两个端点会以不同的方式解释同一个令牌。
from .metrics import (
    ALERTS_PATH,
    ALERTS_URL_NAME,
    PREFIX,
    is_authorised,
    iter_samples,
    plain_sample,
    render_families,
)

logger = logging.getLogger(__name__)

# ---- 规则读写的序列名 ----
# 不在这里重新写字面量：写错了不会报错，只会让一条规则永远不触发，
# 而「永远不触发」和「没有这条规则」在看板上长得一模一样。
METRIC_REQUESTS_TOTAL = PREFIX + '_http_requests_total'
METRIC_DURATION_BUCKET = PREFIX + '_http_request_duration_seconds_bucket'
METRIC_LABEL_OVERFLOW = PREFIX + '_metrics_label_overflow'
METRIC_SHARED_ENABLED = PREFIX + '_metrics_shared_enabled'
METRIC_INSTANCES = PREFIX + '_metrics_instances'
METRIC_VIEW_AGE = PREFIX + '_metrics_view_age_seconds'
METRIC_BUCKET_MISMATCH = PREFIX + '_metrics_bucket_mismatch'
METRIC_READ_ERRORS = PREFIX + '_metrics_shared_read_errors_total'

# ---- 阈值与节奏 ----

# 视图陈旧：默认五秒发布一次，三倍没更新就说明发布线程停了或副本没了。
# 取三倍而不是一倍，是为了让一次 GC 停顿或重启不至于把值班的人叫起来。
VIEW_STALE_SECONDS = 15.0
VIEW_STALE_FOR = 60.0

# 共享读取失败：十分钟内三次以上。抓取间隔通常 15-30 秒，三次等于连续几分
# 钟都在降级，不是一次网络抖动。
READ_ERROR_MIN_INCREASE = 3.0
READ_ERROR_WINDOW = 600.0
READ_ERROR_FOR = 0.0

# 分桶不符：有一份延迟数据没进直方图。for 取两分钟，等一次滚动发布走完——
# 新旧版本同时在线的窗口内这条必然亮着，那正是它该等的东西。
BUCKET_MISMATCH_FOR = 120.0

# 实例数比一小时内的峰值少：一个副本不在了。峰值用一小时的窗口，短了会把
# 正常的缩容也算成故障，长了则记住一次早已结束的扩容。
INSTANCE_WINDOW = 3600.0
INSTANCE_DROP_FOR = 120.0

# 标签基数触顶：此刻指标自己已经在撒谎，必须立刻知道，没有 for。
LABEL_OVERFLOW_FOR = 0.0

# 5xx 比例与 p95：窗口内至少这么多样本才判断，否则凌晨的一次低流量抖动就能
# 把比例推过阈值。300 秒的 for 同理——持续五分钟才算故障。
ERROR_RATE_WINDOW = 600.0
ERROR_RATE_THRESHOLD = 0.05
ERROR_RATE_MIN_SAMPLES = 100.0
ERROR_RATE_FOR = 300.0

LATENCY_WINDOW = 600.0
LATENCY_QUANTILE = 0.95
LATENCY_THRESHOLD = 2.0
LATENCY_MIN_SAMPLES = 50.0
LATENCY_FOR = 300.0

# 没启用共享：你看到的是一个进程。info 级，本地开发会一直亮着——那不是噪声，
# 那是一句正确的提醒。
SHARED_DISABLED_FOR = 600.0

# 历史窗口必须不小于任何规则的窗口，否则规则会「忘了」它还需要的样本。
WINDOW_MAX_AGE = 3900.0


def histogram_quantile(buckets, quantile):
    """从累计分桶求分位数，桶内线性插分。

    ``buckets`` 是 {上界: 累计计数}，``inf`` 键是总样本数。没有 ``inf``
    键时退到最大的有限上界——那等于把 +Inf 桶当成最后一个有限桶，只会
    低估，不会凭空造出一个数。
    """

    if not buckets:
        return None
    total = buckets.get(math.inf)
    if total is None:
        finite = [edge for edge in buckets if edge != math.inf]
        if not finite:
            return None
        total = buckets[max(finite)]
    if total <= 0:
        return None
    target = quantile * total
    lower_edge, lower_count = 0.0, 0.0
    for edge in sorted(edge for edge in buckets if edge != math.inf):
        count = buckets[edge]
        if count >= target:
            if count == lower_count:
                # 这一档是空的，目标落在两档之间：取上界比除零诚实。
                return edge
            return lower_edge + (edge - lower_edge) * (
                (target - lower_count) / (count - lower_count)
            )
        lower_edge, lower_count = edge, count
    return lower_edge


class SampleWindow:
    """保留最近一段时间的指标样本，供需要历史的规则使用。

    Prometheus 那边由 TSDB 承担这件事，这里只有内存里一小段，按秒老化。
    它是个有界结构：序列数由指标侧的封顶决定，时间长度由 ``max_age`` 决定，
    都不会跟着进程一起无限长大。
    """

    def __init__(self, max_age=WINDOW_MAX_AGE):
        self._max_age = float(max_age)
        self._series = {}
        self._lock = threading.Lock()

    def record(self, samples, now):
        current = {}
        for name, labels, value in samples:
            current[(name, tuple(sorted(labels.items())))] = float(value)
        with self._lock:
            for key, value in current.items():
                self._series.setdefault(key, deque()).append((now, value))
            self._prune(now)

    def _prune(self, now):
        cutoff = now - self._max_age
        for key in list(self._series):
            entries = self._series[key]
            while entries and entries[0][0] < cutoff:
                entries.popleft()
            if not entries:
                del self._series[key]

    def _keys(self, name, labels=None):
        for key in self._series:
            if key[0] != name:
                continue
            if labels and any(
                dict(key[1]).get(expected_key) != expected
                for expected_key, expected in labels.items()
            ):
                continue
            yield key

    def _value_at(self, key, cutoff):
        """窗口起点上的值；起点之前没有样本时退到窗口内最旧的那条。

        退这一步是为了冷启动。严格按起点取基线的话，进程刚起来的十分钟里
        窗口内每一条样本都比起点新，错误率和延迟规则会整整十分钟不判断——
        而这两条规则用的都是同一窗口内两个增量的比值，窗口短一点也不影响
        结论；读取失败那条是计数，窗口短了只会少算，不会误报。

        仍然保留「一条样本算不出增量」：只有一条时返回 None。「没有历史」
        和「增量为 0」必须分开，否则一次重启之后的第一次抓取会报告
        「十分钟内零个 5xx」——那是一句没法证明的话。
        """

        entries = self._series.get(key)
        if not entries:
            return None
        found = None
        for timestamp, value in entries:
            if timestamp <= cutoff:
                found = value
            else:
                break
        if found is None and len(entries) > 1:
            return entries[0][1]
        return found

    def increase(self, name, window, now, labels=None):
        """窗口内的增量合计；取不到基线的序列不计入。"""

        total = 0.0
        seen = False
        for key in self._keys(name, labels):
            entries = self._series[key]
            baseline = self._value_at(key, now - window)
            if baseline is None:
                continue
            # 计数器会因进程重启归零。负增量按 0 计：把重启算成「流量消失」
            # 比算成「流量为负」合理，后者在看板上是一条向下的假跌。
            total += max(0.0, entries[-1][1] - baseline)
            seen = True
        return total if seen else None

    def peak(self, name, window, now, labels=None):
        """窗口内出现过的最大值。"""

        best = None
        for key in self._keys(name, labels):
            for timestamp, value in self._series[key]:
                if timestamp < now - window:
                    continue
                if best is None or value > best:
                    best = value
        return best

    def histogram(self, name, window, now):
        """窗口内按 ``le`` 汇总的直方图增量，跨其余标签求和。

        跨 (method, route) 求和是故意的：告警关心的是「整个服务有多慢」，
        不是某一条路由。按路由告警会把告警序列数乘以路由数，那正是指标侧
        努力避免的事情。
        """

        buckets = {}
        for key in self._keys(name):
            labels = dict(key[1])
            edge = labels.pop('le', None)
            if edge is None:
                continue
            entries = self._series[key]
            baseline = self._value_at(key, now - window)
            if baseline is None:
                continue
            boundary = math.inf if edge == '+Inf' else float(edge)
            buckets[boundary] = (
                buckets.get(boundary, 0.0) + max(0.0, entries[-1][1] - baseline)
            )
        return buckets


class RuleContext:
    """一条规则在这一次评估里能看到的东西。"""

    def __init__(self, samples, window, now):
        self._samples = list(samples)
        self._window = window
        self.now = now

    def gauge(self, name, **labels):
        """当前值；没有这个序列时返回 None，而不是 0。

        返回 0 的话，「没有这个序列」和「这个序列是 0」会变成同一件事，
        而前者意味着输入根本不对，规则不该拿它下任何结论。
        """

        total = 0.0
        found = False
        for sample_name, sample_labels, value in self._samples:
            if sample_name != name:
                continue
            if any(
                sample_labels.get(key) != expected
                for key, expected in labels.items()
            ):
                continue
            total += value
            found = True
        return total if found else None

    def increase(self, name, window, **labels):
        return self._window.increase(name, window, self.now, labels or None)

    def peak(self, name, window, **labels):
        return self._window.peak(name, window, self.now, labels or None)

    def histogram(self, name, window):
        return self._window.histogram(name, window, self.now)


class Rule:
    """一条告警规则。

    ``for_seconds`` 是 Prometheus 的 pending 语义：条件连续成立这么久才升级
    为 firing。一次抖动不该喊人，一次持续故障必须喊——所以每条规则的 for 都
    按「这个现象自己恢复的概率有多大」取，不按感觉取。
    """

    def __init__(self, name, severity, summary, for_seconds, probe):
        self.name = name
        self.severity = severity
        self.summary = summary
        self.for_seconds = float(for_seconds)
        self._probe = probe

    def probe(self, context):
        """返回 (是否越界, 观测值, 给人看的一句话)。"""

        return self._probe(context)


class _RuleState:
    __slots__ = ('pending_since', 'firing_since', 'resolved_at', 'value', 'detail')

    def __init__(self):
        self.pending_since = None
        self.firing_since = None
        self.resolved_at = None
        self.value = None
        self.detail = ''


# ---- 各条规则的探针 ----
# 每个探针返回三元组而不是布尔值：只有布尔值的话，看板上永远只有「告警了」
# 三个字，值班的人还得自己去猜是哪个数越界、越了多少。


def _probe_view_stale(context):
    age = context.gauge(METRIC_VIEW_AGE)
    if age is None:
        return False, None, '视图里没有年龄序列，无法判断'
    if age <= VIEW_STALE_SECONDS:
        return False, age, '最旧的远端快照 %.1f 秒前更新' % age
    return True, age, (
        '最旧的远端快照已经 %.1f 秒没有更新（阈值 %.0f 秒）：'
        '发布线程停了，或者那个副本已经不在服务' % (age, VIEW_STALE_SECONDS)
    )


def _probe_read_errors(context):
    increase = context.increase(METRIC_READ_ERRORS, READ_ERROR_WINDOW)
    if increase is None:
        return False, None, '还没有足够的历史判断增速'
    if increase < READ_ERROR_MIN_INCREASE:
        return False, increase, '最近十分钟读取共享存储失败 %d 次' % increase
    return True, increase, (
        '最近十分钟读取共享存储失败 %d 次（阈值 %d）：'
        '舰队视图正在悄悄退化成本进程视图' % (increase, READ_ERROR_MIN_INCREASE)
    )


def _probe_bucket_mismatch(context):
    mismatch = context.gauge(METRIC_BUCKET_MISMATCH)
    if not mismatch:
        return False, mismatch or 0, '没有实例因分桶布局不同被跳过'
    return True, mismatch, (
        '%d 个实例的直方图分桶与本地不同，它们的延迟没有进直方图'
        '（请求数仍然计入总数）' % mismatch
    )


def _probe_instances_dropped(context):
    instances = context.gauge(METRIC_INSTANCES)
    peak = context.peak(METRIC_INSTANCES, INSTANCE_WINDOW)
    if instances is None or peak is None or peak <= 1:
        return False, instances, '一小时内没有出现过多个实例，无从比较'
    if instances >= peak:
        return False, instances, '当前 %d 个实例，与一小时内峰值相同' % instances
    return True, instances, (
        '当前 %d 个实例，一小时内的峰值是 %d：有副本不在了' % (instances, peak)
    )


def _probe_label_overflow(context):
    overflow = context.gauge(METRIC_LABEL_OVERFLOW)
    if not overflow:
        return False, overflow or 0, '标签组合没有触顶'
    return True, overflow, (
        '标签组合已触及序列上限，新组合被并入 overflow 路由：'
        '此刻的指标已经不全，看什么都会低估'
    )


def _probe_error_rate(context):
    total = context.increase(METRIC_REQUESTS_TOTAL, ERROR_RATE_WINDOW)
    errors = context.increase(
        METRIC_REQUESTS_TOTAL, ERROR_RATE_WINDOW, status_class='5xx')
    if total is None or errors is None or total < ERROR_RATE_MIN_SAMPLES:
        return False, total, '窗口内样本不足 %d，不做判断' % ERROR_RATE_MIN_SAMPLES
    rate = errors / total
    if rate <= ERROR_RATE_THRESHOLD:
        return False, rate, '十分钟内 5xx 占比 %.2f%%' % (rate * 100)
    return True, rate, (
        '十分钟内 5xx 占比 %.2f%%（阈值 %.0f%%，样本 %d）：'
        '用户正在看到错误页' % (
            rate * 100, ERROR_RATE_THRESHOLD * 100, total,
        )
    )


def _probe_latency(context):
    buckets = context.histogram(METRIC_DURATION_BUCKET, LATENCY_WINDOW)
    count = buckets.get(math.inf, 0.0)
    if count < LATENCY_MIN_SAMPLES:
        return False, None, '窗口内样本不足 %d，不做判断' % LATENCY_MIN_SAMPLES
    value = histogram_quantile(buckets, LATENCY_QUANTILE)
    if value is None or value <= LATENCY_THRESHOLD:
        return False, value, '十分钟内 p95 为 %.3f 秒' % (value or 0.0)
    return True, value, (
        '十分钟内 p95 为 %.3f 秒（阈值 %.1f 秒，样本 %d）'
        % (value, LATENCY_THRESHOLD, count)
    )


def _probe_shared_disabled(context):
    if context.gauge(METRIC_SHARED_ENABLED):
        return False, 1, '已启用跨副本共享'
    return True, 0, (
        '未启用跨副本共享：上面全部数字只是这一个进程的，'
        '多副本部署下会随负载均衡的结果跳动'
    )


RULES = (
    Rule('MetricsFleetViewStale', 'warning',
         '跨副本指标视图已过期，舰队视图停在旧快照',
         VIEW_STALE_FOR, _probe_view_stale),
    Rule('MetricsSharedReadErrorsRising', 'warning',
         '读取指标共享存储连续失败，舰队视图正在退化',
         READ_ERROR_FOR, _probe_read_errors),
    Rule('MetricsBucketMismatch', 'warning',
         '有实例的直方图分桶与本地不同，其延迟数据未并入直方图',
         BUCKET_MISMATCH_FOR, _probe_bucket_mismatch),
    Rule('MetricsFleetShrunk', 'warning',
         '可见实例数比一小时内少，有副本不在服务',
         INSTANCE_DROP_FOR, _probe_instances_dropped),
    Rule('MetricsLabelOverflow', 'warning',
         '标签组合触及序列上限，指标已经不全',
         LABEL_OVERFLOW_FOR, _probe_label_overflow),
    Rule('MetricsServerErrorRateHigh', 'critical',
         '5xx 占比超过阈值，用户正在看到错误页',
         ERROR_RATE_FOR, _probe_error_rate),
    Rule('MetricsLatencyP95High', 'warning',
         '请求耗时 p95 超过阈值',
         LATENCY_FOR, _probe_latency),
    Rule('MetricsSharedDisabled', 'info',
         '未启用跨副本共享，上面全部数字只是单个进程的',
         SHARED_DISABLED_FOR, _probe_shared_disabled),
)


class AlertEngine:
    """按规则评估样本，维护 pending → firing 的状态机。"""

    def __init__(self, rules=RULES, window=None):
        self._rules = tuple(rules)
        self._window = SampleWindow() if window is None else window
        # 状态按需建，不在这里按当次的规则集合一次性建齐：两份集合一旦漂移
        # 就是一次 KeyError，而它只在运行期显形，启动时一点声音都没有。
        self._states = {}
        self._lock = threading.Lock()
        self._evaluations = 0
        self._errors = 0
        self._last = []

    def evaluate(self, samples, now=None):
        """评估一次，返回按规则顺序排列的状态列表。"""

        now = time.time() if now is None else now
        with self._lock:
            self._evaluations += 1
        try:
            self._window.record(samples, now)
            context = RuleContext(samples, self._window, now)
            results = []
            for rule in self._rules:
                fired, value, detail = rule.probe(context)
                with self._lock:
                    results.append(self._advance(rule, fired, value, detail, now))
        except Exception as error:
            # 告警自己坏掉不能顺带把 /metrics/ 搞成 500：一个挂掉的监控端点比
            # 没有告警更难排查。这里退回上一次的结论并计数。
            with self._lock:
                self._errors += 1
            logger.warning(
                '告警评估失败，沿用上一次的结论：%s: %s',
                type(error).__name__, error,
            )
            return self.states()
        with self._lock:
            self._last = results
        return results

    def states(self):
        with self._lock:
            return [dict(item) for item in self._last]

    def counters(self):
        with self._lock:
            return {'evaluations': self._evaluations, 'errors': self._errors}

    def _advance(self, rule, fired, value, detail, now):
        state = self._states.get(rule.name)
        if state is None:
            state = self._states[rule.name] = _RuleState()
        state.value = value
        state.detail = detail
        if not fired:
            if state.pending_since is not None or state.firing_since is not None:
                state.resolved_at = now
            state.pending_since = None
            state.firing_since = None
            return self._view(rule, state, 'inactive', now)
        if state.pending_since is None:
            state.pending_since = now
        if now - state.pending_since >= rule.for_seconds:
            if state.firing_since is None:
                state.firing_since = state.pending_since
                state.resolved_at = None
            return self._view(rule, state, 'firing', now)
        return self._view(rule, state, 'pending', now)

    def _view(self, rule, state, status, now):
        since = state.firing_since if status == 'firing' else state.pending_since
        return {
            'name': rule.name,
            'severity': rule.severity,
            'summary': rule.summary,
            'state': status,
            'value': state.value,
            'detail': state.detail,
            'for_seconds': rule.for_seconds,
            'since': since,
            'active_seconds': (now - since) if since is not None else 0.0,
        }


_engine = None
_engine_lock = threading.Lock()


def get_engine():
    """进程内唯一的评估器。"""

    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = AlertEngine()
    return _engine


def reset_engine():
    """换一个空的评估器。测试之间需要干净起点时才用。"""

    global _engine
    with _engine_lock:
        _engine = AlertEngine()
    return _engine


def _alert_labels(state):
    return (('alertname', state['name']), ('severity', state['severity']))


def _alert_families(states, counters):
    """把告警状态整理成与 :func:`campus_glimmer.metrics._families` 同形的家族。

    样本统一走 metrics 的 :func:`plain_sample`：同一个形状意味着
    ``render_families`` 和 ``iter_samples`` 不需要为告警单开一条分支，而
    分支正是两侧口径漂移开始的地方。
    """

    return [
        (
            PREFIX + '_alert_firing', 'gauge',
            '当前处于 firing 状态的告警，按告警名与严重程度区分。',
            [plain_sample(_alert_labels(state), 1)
             for state in states if state['state'] == 'firing'],
        ),
        (
            PREFIX + '_alert_pending', 'gauge',
            '条件已成立但还未熬过 for 时长的告警数。',
            [plain_sample(_alert_labels(state), 1)
             for state in states if state['state'] == 'pending'],
        ),
        (
            PREFIX + '_alert_active_seconds', 'gauge',
            '条件连续成立的秒数；firing 时即故障已持续的时长。',
            [plain_sample(_alert_labels(state), state['active_seconds'])
             for state in states if state['state'] != 'inactive'],
        ),
        (
            PREFIX + '_alert_evaluations_total', 'counter',
            '告警评估完成的次数。',
            [plain_sample((), counters['evaluations'])],
        ),
        (
            PREFIX + '_alert_evaluation_errors_total', 'counter',
            '告警评估自身抛出异常的次数；非 0 时上面的结论可能已经过期。',
            [plain_sample((), counters['errors'])],
        ),
    ]


def render_alerts(samples, now=None):
    """把当前告警状态渲染成 Prometheus 文本，由 /metrics/ 拼在末尾。"""

    engine = get_engine()
    states = engine.evaluate(samples, now=now)
    return render_families(_alert_families(states, engine.counters()))


def _current_gauges(samples):
    gauges = {}
    for name, labels, value in samples:
        if labels:
            continue
        gauges[name] = value
    return gauges


def alerts_payload(states, samples, now):
    gauges = _current_gauges(samples)
    counts = {'firing': 0, 'pending': 0, 'inactive': 0}
    for state in states:
        counts[state['state']] += 1
    return {
        'service': settings.METRICS_SERVICE_NAME,
        'generated_at': now,
        'shared': bool(gauges.get(METRIC_SHARED_ENABLED, 0)),
        'instances': gauges.get(METRIC_INSTANCES, 1),
        'counts': counts,
        'alerts': states,
    }


@require_http_methods(['GET', 'HEAD'])
def alerts_view(request):
    """把人看的告警状态暴露成 JSON。

    与 /metrics/ 同一把令牌：两个端点回答的是同一批问题的不同侧面，权限
    不该分成两套。
    """

    if not is_authorised(request):
        raise Http404('告警端点未启用')
    from .metrics import fleet_view

    now = time.time()
    view = fleet_view()
    states = get_engine().evaluate(iter_samples(view), now=now)
    response = JsonResponse(
        alerts_payload(states, iter_samples(view), now),
        json_dumps_params={'ensure_ascii': False},
    )
    response.headers['Cache-Control'] = 'no-store'
    return response