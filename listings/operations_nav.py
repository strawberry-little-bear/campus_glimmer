# -*- coding: utf-8 -*-
"""运营看板的分区导航数据。

看板由 32 个 partial 顺序拼成，一屏滚不到头，此前没有任何分区入口，运营想找
「需求雷达」只能一路滚过去。这里按主题把 32 个分区分成 6 组，模板据此渲染顶
部跳转条。

两条约定，``test_operations_nav`` 会逐条盯住：

* 分组按主题划分，组的先后顺序不代表页面顺序——页面本身把同一主题的分区打散
  在了多处（「交易与交付」就横跨第 11 到第 25 个分区）。
* **组内严格按页面顺序**。用户在组内从左往右扫，方向和他往下滚是一致的；一旦
  组内乱序，chip 看起来就会像随机排列。

锚点与 ``operations_dashboard.html`` 里包裹各 include 的 ``<section id>`` 一一
对应。两边分开书写，所以测试会渲染真实页面，断言导航声明的每个锚点都能落到元
素上、页面上每个分区也都登记在案，防止改名后静默失效。
"""

OPERATIONS_DASHBOARD_SECTIONS = (
    ("总览", (
        ("ops-alerts", "运营提醒"),
        ("ops-metrics", "核心指标"),
        ("ops-funnel", "转化漏斗"),
        ("ops-charts", "活动与交易"),
    )),
    ("需求与搜索", (
        ("ops-demand", "校园需求"),
        ("ops-demand-radar", "需求雷达"),
        ("ops-saved-search", "关注搜索"),
        ("ops-search-exposure", "搜索曝光"),
    )),
    ("交易与交付", (
        ("ops-order-lifecycle", "订单链路"),
        ("ops-borrow", "借用催收"),
        ("ops-meeting", "面交时段"),
        ("ops-handover-codes", "交接码"),
        ("ops-delivery-confirm", "交付确认"),
        ("ops-trade-quality", "交易质量"),
        ("ops-deal-outcomes", "成交结果"),
    )),
    ("内容与治理", (
        ("ops-governance", "治理公平"),
        ("ops-lost-found", "失物招领"),
        ("ops-campus-verify", "校园认证"),
        ("ops-queues", "运营队列"),
        ("ops-moderation", "审核与申诉"),
    )),
    ("用户与增长", (
        ("ops-notifications", "通知触达"),
        ("ops-campaigns", "校园专题"),
        ("ops-response", "响应时效"),
        ("ops-recommendations", "推荐质量"),
        ("ops-contribution", "贡献体系"),
    )),
    ("系统与参数", (
        ("ops-semester-phase", "学期阶段"),
        ("ops-reminders", "提醒转化"),
        ("ops-scheduler", "定时任务"),
        ("ops-item-refresh", "商品刷新"),
        ("ops-price-signal", "价格信号"),
        ("ops-audit-shapes", "参数形状"),
        ("ops-locations", "地点档案"),
    )),
)
