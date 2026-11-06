# campus_glimmer/alerts.py
"""按指标的阈值告警：把「现在有没有越界」从人的眼睛搬到代码里。

只评估、报告，并在状态翻转时可选地 POST 一次 Webhook：不维护值班表、
不发邮件、不改任何业务状态。产物是 ``/alerts/`` 上的一份 JSON、``/metrics/``
末尾的几个 ``campus_glimmer_alert_*`` 序列，以及一条可选的批量通知。通知只在
pending → firing 和 firing → inactive 时触发，持续 firing 不会每轮抓取重复喊人。

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

import json
import logging
import math
import threading
import time
import urllib.request
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
    instance_id,
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

# outbox 的状态标签必须固定：某类状态暂时为 0 时也输出样本，否则 Prometheus
# 里这条序列会消失，告警表达式和看板会随队列内容改变形状。
ALERT_DELIVERY_STATUSES = (
    'pending',
    'delivering',
    'delivered',
    'skipped',
    'exhausted',
)

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

# Webhook 单次请求必须足够短，即使远端挂住也不能把监控端点拖过常见 scrape
# timeout。配置数据库 outbox 后，失败由管理命令重试；没有 outbox 时才退回
# 原先的一次性尽力而为语义。
WEBHOOK_TIMEOUT_SECONDS = 2.0

# 最近一次进程生命周期内的状态翻转只保留一个有界尾部。它是给人排障的窗口，
# 不是持久化审计：进程重启即清空，多副本之间也不共享。固定上限避免告警反复
# 翻转时把监控进程自己的内存拖垮。
ALERT_HISTORY_LIMIT = 100


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


class AlertNotifier:
    """把一批状态翻转投递成一次 JSON POST。

    URL 未配置时什么都不发，但把事件记成 ``skipped``；配置了却失败时记成
    ``failed``。两种情况都不抛给调用方——监控端点不能因为喊人的渠道坏了而
    返回 500。是否持久化、重试与去重由调用方的 outbox 决定；通知器本身只
    负责一次网络请求。
    """

    def __init__(self, url='', token='', timeout=WEBHOOK_TIMEOUT_SECONDS,
                 service_name=None, opener=None):
        self._url = (url or '').strip()
        self._token = (token or '').strip()
        self._timeout = max(float(timeout), 0.1)
        self._service_name = (
            service_name
            or getattr(settings, 'METRICS_SERVICE_NAME', 'campus-glimmer')
        )
        self._opener = opener or urllib.request.urlopen

    @classmethod
    def from_settings(cls):
        return cls(
            url=getattr(settings, 'ALERT_WEBHOOK_URL', ''),
            token=getattr(settings, 'ALERT_WEBHOOK_BEARER_TOKEN', ''),
            timeout=getattr(settings, 'ALERT_WEBHOOK_TIMEOUT', WEBHOOK_TIMEOUT_SECONDS),
            service_name=getattr(settings, 'METRICS_SERVICE_NAME', 'campus-glimmer'),
        )

    def deliver(self, events, now):
        """返回按结果拆分的事件计数；网络失败永远不向外抛。"""

        if not events:
            return {'delivered': 0, 'failed': 0, 'skipped': 0}
        if not self._url:
            return {'delivered': 0, 'failed': 0, 'skipped': len(events)}

        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'User-Agent': 'campus-glimmer-alerts/1',
        }
        if self._token:
            headers['Authorization'] = 'Bearer ' + self._token
        body = json.dumps(
            {
                'service': self._service_name,
                'generated_at': now,
                'events': events,
            },
            ensure_ascii=False,
            separators=(',', ':'),
        ).encode('utf-8')
        request = urllib.request.Request(
            self._url, data=body, headers=headers, method='POST',
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                status = getattr(response, 'status', 200)
                if not 200 <= int(status) < 300:
                    raise RuntimeError('webhook returned HTTP %s' % status)
        except Exception as error:
            # 不把 URL 或响应正文写进日志：Webhook 地址本身可能带访问凭据。
            logger.warning(
                '告警 Webhook 投递失败（%d 条）：%s',
                len(events), type(error).__name__,
            )
            return {'delivered': 0, 'failed': len(events), 'skipped': 0}
        return {'delivered': len(events), 'failed': 0, 'skipped': 0}


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

    def __init__(self, rules=RULES, window=None, notifier=None, history_store=None,
                 delivery_queue=None):
        self._rules = tuple(rules)
        self._window = SampleWindow() if window is None else window
        self._notifier = notifier
        self._history_store = history_store
        self._delivery_queue = delivery_queue
        # 状态按需建，不在这里按当次的规则集合一次性建齐：两份集合一旦漂移
        # 就是一次 KeyError，而它只在运行期显形，启动时一点声音都没有。
        self._states = {}
        self._lock = threading.Lock()
        self._evaluations = 0
        self._errors = 0
        self._notifications = {
            'delivered': 0, 'failed': 0, 'skipped': 0, 'deduplicated': 0,
        }
        self._transitions = {'firing': 0, 'resolved': 0}
        self._history_errors = {'write': 0, 'read': 0}
        self._history = deque(maxlen=ALERT_HISTORY_LIMIT)
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
            notifications = []
            for rule in self._rules:
                fired, value, detail = rule.probe(context)
                with self._lock:
                    state, notification = self._advance(
                        rule, fired, value, detail, now,
                    )
                results.append(state)
                if notification is not None:
                    notifications.append(notification)
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
        self._persist_events(notifications)
        if notifications and self._notifier is not None:
            self._deliver_notifications(notifications, now)
        return results

    def _persist_events(self, events):
        """把翻转写入外部历史；失败只降级历史，不影响评估和通知。"""

        if self._history_store is None or not events:
            return
        for event in events:
            try:
                self._history_store.record(event)
            except Exception as error:
                with self._lock:
                    self._history_errors['write'] += 1
                logger.warning(
                    '告警历史写入失败，保留进程内记录：%s: %s',
                    type(error).__name__, error,
                )

    def _deliver_notifications(self, events, now):
        outcomes = None
        if self._delivery_queue is not None:
            try:
                from datetime import datetime, timezone as datetime_timezone

                outcomes = self._delivery_queue.enqueue_and_deliver(
                    events,
                    self._notifier,
                    now=datetime.fromtimestamp(now, tz=datetime_timezone.utc),
                )
            except Exception as error:
                # 队列数据库故障不能让告警通知彻底消失：退回一次直接投递，
                # 同时不让监控端点因为一个附属存储而返回 500。
                logger.warning(
                    '告警 outbox 不可用，退回一次直接投递（%d 条）：%s',
                    len(events), type(error).__name__,
                )
        if outcomes is None:
            try:
                outcomes = self._notifier.deliver(events, now)
                if not isinstance(outcomes, dict):
                    raise TypeError('notifier result must be a dict')
            except Exception as error:
                # 通知器是监控端点的下游，不是它的前置条件。即使测试替身或未来
                # 实现漏掉了异常处理，也只能让这次投递失败，不能把 /metrics/ 拽成 500。
                logger.warning(
                    '告警 Webhook 通知器异常（%d 条）：%s',
                    len(events), type(error).__name__,
                )
                outcomes = {'failed': len(events)}
        with self._lock:
            for result in self._notifications:
                self._notifications[result] += outcomes.get(result, 0)

    def states(self):
        with self._lock:
            return [dict(item) for item in self._last]

    def history(self):
        """返回最近的状态翻转，最新在前；调用方拿到的是副本。"""

        return self.history_snapshot()['events']

    def history_snapshot(self):
        """返回历史及其真实来源；持久化故障时自动退回进程内窗口。"""

        with self._lock:
            local = [dict(event) for event in reversed(self._history)]
            write_errors = self._history_errors['write']
        store = self._history_store
        if store is None:
            return {
                'events': local,
                'scope': 'process',
                'persistent': False,
                'degraded': False,
                'retention_limit': None,
            }
        try:
            persisted = store.recent(ALERT_HISTORY_LIMIT)
        except Exception as error:
            with self._lock:
                self._history_errors['read'] += 1
            logger.warning(
                '告警历史读取失败，退回进程内记录：%s: %s',
                type(error).__name__, error,
            )
            return {
                'events': local,
                'scope': 'process',
                'persistent': False,
                'degraded': True,
                'retention_limit': getattr(store, 'retention_limit', None),
            }
        return {
            'events': self._merge_history(persisted, local),
            'scope': 'database',
            'persistent': True,
            'degraded': write_errors > 0,
            'retention_limit': getattr(store, 'retention_limit', None),
        }

    @staticmethod
    def _merge_history(persisted, local):
        """数据库成功读回时也保留尚未写成功的最近内存事件。"""

        merged = {}
        for event in list(persisted) + list(local):
            key = (
                event.get('event'), event.get('name'), event.get('occurred_at'),
                event.get('instance'), event.get('state'),
            )
            merged.setdefault(key, dict(event))
        return sorted(
            merged.values(),
            key=lambda event: event.get('occurred_at') or 0.0,
            reverse=True,
        )[:ALERT_HISTORY_LIMIT]

    def counters(self):
        with self._lock:
            return {
                'evaluations': self._evaluations,
                'errors': self._errors,
                'notifications': dict(self._notifications),
                'transitions': dict(self._transitions),
                'history_errors': dict(self._history_errors),
            }

    def delivery_snapshot(self):
        """返回 Webhook outbox 摘要；未启用或存储故障时也不影响端点。"""

        queue = self._delivery_queue
        if queue is None:
            return {
                'enabled': False,
                'degraded': False,
                'due': 0,
                'status': {},
            }
        try:
            return queue.snapshot()
        except Exception as error:
            logger.warning(
                '告警 outbox 摘要读取失败：%s', type(error).__name__,
            )
            return {
                'enabled': True,
                'degraded': True,
                'due': 0,
                'status': {},
            }

    def _advance(self, rule, fired, value, detail, now):
        state = self._states.get(rule.name)
        if state is None:
            state = self._states[rule.name] = _RuleState()
        state.value = value
        state.detail = detail
        if not fired:
            was_firing = state.firing_since is not None
            started_at = state.pending_since
            active_seconds = (now - started_at) if started_at is not None else 0.0
            if state.pending_since is not None or state.firing_since is not None:
                state.resolved_at = now
            state.pending_since = None
            state.firing_since = None
            view = self._view(rule, state, 'inactive', now)
            notification = None
            if was_firing:
                notification = self._transition_event(
                    'resolved', rule, state, now, started_at, active_seconds,
                )
            return view, notification
        if state.pending_since is None:
            state.pending_since = now
        if now - state.pending_since >= rule.for_seconds:
            notification = None
            if state.firing_since is None:
                state.firing_since = state.pending_since
                state.resolved_at = None
                notification = self._transition_event(
                    'firing', rule, state, now, state.pending_since,
                    now - state.pending_since,
                )
            return self._view(rule, state, 'firing', now), notification
        return self._view(rule, state, 'pending', now), None

    def _transition_event(self, transition, rule, state, now, since,
                          active_seconds):
        event = {
            'event': transition,
            'name': rule.name,
            'severity': rule.severity,
            'summary': rule.summary,
            'state': 'firing' if transition == 'firing' else 'inactive',
            'value': state.value,
            'detail': state.detail,
            'since': since,
            'occurred_at': now,
            'active_seconds': active_seconds,
            'instance': instance_id(),
        }
        # 历史和通知各拿一份副本：通知器是外部边界，不能让它的实现有机会
        # 改动排障窗口里的记录。
        self._history.append(dict(event))
        self._transitions[transition] += 1
        return dict(event)

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


def _new_engine():
    history_store = None
    if getattr(settings, 'ALERT_HISTORY_DB_ENABLED', True):
        from observability.history import DatabaseAlertHistory

        history_store = DatabaseAlertHistory()
    delivery_queue = None
    if (
        getattr(settings, 'ALERT_NOTIFICATION_QUEUE_ENABLED', True)
        and (getattr(settings, 'ALERT_WEBHOOK_URL', '') or '').strip()
    ):
        from observability.delivery import AlertDeliveryQueue

        delivery_queue = AlertDeliveryQueue.from_settings()
    return AlertEngine(
        notifier=AlertNotifier.from_settings(),
        history_store=history_store,
        delivery_queue=delivery_queue,
    )


def get_engine():
    """进程内唯一的评估器。"""

    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = _new_engine()
    return _engine


def reset_engine():
    """换一个空的评估器。测试之间需要干净起点时才用。"""

    global _engine
    with _engine_lock:
        _engine = _new_engine()
    return _engine


def _alert_labels(state):
    return (('alertname', state['name']), ('severity', state['severity']))


def _alert_families(states, counters, delivery=None):
    """把告警状态整理成与 :func:`campus_glimmer.metrics._families` 同形的家族。

    样本统一走 metrics 的 :func:`plain_sample`：同一个形状意味着
    ``render_families`` 和 ``iter_samples`` 不需要为告警单开一条分支，而
    分支正是两侧口径漂移开始的地方。

    ``delivery`` 缺省为关闭状态，只给直接构造告警家族的测试与调用方使用；
    ``/metrics/`` 始终传入同一个 ``delivery_snapshot()``，避免一次抓取查询
    两次 outbox 后看见两份互相矛盾的数字。
    """

    if delivery is None:
        delivery = {
            'enabled': False,
            'degraded': False,
            'due': 0,
            'status': {},
        }
    delivery_status = delivery.get('status') or {}

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
            PREFIX + '_alert_notification_queue', 'gauge',
            '告警 Webhook outbox 中的记录数，按 pending/delivering/delivered/skipped/exhausted 区分。',
            [plain_sample((('status', status),), delivery_status.get(status, 0))
             for status in ALERT_DELIVERY_STATUSES],
        ),
        (
            PREFIX + '_alert_notification_queue_due', 'gauge',
            '告警 Webhook outbox 中已到重试时间、等待投递的记录数。',
            [plain_sample((), delivery.get('due', 0))],
        ),
        (
            PREFIX + '_alert_notification_queue_enabled', 'gauge',
            '是否启用了数据库 Webhook outbox；0 表示通知未配置或退回一次性直投。',
            [plain_sample((), 1 if delivery.get('enabled') else 0)],
        ),
        (
            PREFIX + '_alert_notification_queue_degraded', 'gauge',
            'outbox 摘要读取是否失败；为 1 时下面的状态计数均不可信。',
            [plain_sample((), 1 if delivery.get('degraded') else 0)],
        ),
        (
            PREFIX + '_alert_notifications_total', 'counter',
            '告警状态翻转时的 Webhook 投递事件数，按 delivered/failed/skipped 区分。',
            [plain_sample((('result', result),), counters['notifications'][result])
             for result in ('delivered', 'failed', 'skipped')],
        ),
        (
            PREFIX + '_alert_transitions_total', 'counter',
            '告警真实状态翻转的次数，按 firing/resolved 区分。',
            [plain_sample((('event', event),), counters['transitions'][event])
             for event in ('firing', 'resolved')],
        ),
        (
            PREFIX + '_alert_history_errors_total', 'counter',
            '告警历史持久化写入或读取失败的次数，按 operation 区分。',
            [plain_sample((('operation', operation),), counters['history_errors'][operation])
             for operation in ('write', 'read')],
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
    delivery = engine.delivery_snapshot()
    return render_families(_alert_families(states, engine.counters(), delivery))


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
    history = get_engine().history_snapshot()
    return {
        'service': settings.METRICS_SERVICE_NAME,
        'generated_at': now,
        'shared': bool(gauges.get(METRIC_SHARED_ENABLED, 0)),
        'instances': gauges.get(METRIC_INSTANCES, 1),
        'counts': counts,
        'alerts': states,
        'history': history['events'],
        'history_limit': ALERT_HISTORY_LIMIT,
        'history_retention': history['retention_limit'],
        'history_scope': history['scope'],
        'history_persistent': history['persistent'],
        'history_degraded': history['degraded'],
        'notification_queue': get_engine().delivery_snapshot(),
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