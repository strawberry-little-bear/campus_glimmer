# -*- coding: utf-8 -*-
"""运营看板分区的按需取回表：锚点 -> 渲染这个分区需要什么。

看板 38 个分区一屏滚不到头，整页 HTML 有 600KB 以上。``data-ops-deferred``
最早只是「CSS 藏起来、滚到再揭开」，服务端一个字节都没少渲染；那把服务端也切开：
带着 ``ops_lazy`` cookie 来的请求只渲染首屏四个分区，其余 34 个只留一个空
``<section>`` 和一条取回地址，滚到附近再由前端去拿（见 ``operations_dashboard_fragment``
和 main.js 里那段）。

切开的代价是服务端必须知道「渲染这个分区需要哪些上下文」。这个文件就是那张表。

表是手维护的，所以 ``test_operations_fragments`` 有一条反查测试：拿一次真实
渲染插桩，看模板到底读了哪些顶层键，再和这张表对。模板新增一个键而表里没登记，
那条测试就红——不会出现「面板悄悄少了一半数据」这种只在生产环境看得见的 bug。
历史上 ``ops-charts`` 的 ``trend_max`` 就是静态扫不到的：它出现在
``{% widthratio %}`` 的参数里，任何按 ``{{ }}`` 抓变量的脚本都会漏。

三条约定：
* 表的顺序就是页面顺序。它和 ``operations_nav.OPERATIONS_DASHBOARD_SECTIONS`` 不是
  同一套顺序——后者按主题分组（「交易与交付」在页面上横跨第 16 到第 31 个分区），
  前者就是模板里的物理顺序。跳转条按导航顺序排，分区按页面顺序排，两边本来就不同；
* ``needs_core`` 为真才跑 ``build_operations_dashboard``。它是整座看板最贵的一部
  分，为一个只显示「失物匹配结果」的分区跑一遍纯属浪费；
* ``ALL_SHARED_PANELS`` 表示「共享面板全跑」。
"""

# 「全部共享面板」的简写。
ALL_SHARED_PANELS = '*'


# (锚点, partial, 是否需要核心统计, 需要的共享面板键)。
# 顺序即页面顺序。
OPERATIONS_SECTION_FRAGMENTS = (
    ('ops-alerts', '_alerts.html', True, ()),
    ('ops-metrics', '_overview_metrics.html', True, ()),
    ('ops-overview', '_overview.html', True, ()),
    ('ops-charts', '_overview_charts.html', True, ()),
    ('ops-demand-match', '_demand_match.html', True, ('demand_match_quality',)),
    ('ops-demand', '_demand.html', True, ('demand_opportunity_followups', 'demand_task_aging')),
    ('ops-notification-reach', '_notification_reach.html', True, ()),
    ('ops-notifications', '_notifications.html', False, ('notification_aggregation_quality', 'notification_kind_coverage', 'notification_preference_bypass_diagnosis', 'notification_preference_usage', 'notification_quiet_hours_shape', 'notification_snooze_return')),
    ('ops-governance', '_governance.html', False, ('meeting_incident_quality', 'moderation_notice_preference', 'report_resolution_quality')),
    ('ops-campaigns', '_campaigns.html', True, ('campaign_phase_review', 'campaign_digest_shape')),
    ('ops-response', '_response.html', True, ()),
    ('ops-recommendations', '_recommendations.html', False, ('favorite_collection_quality', 'opportunity_dismissal_quality', 'opportunity_impression_quality', 'recommendation_impression_quality', 'recommendation_quality')),
    ('ops-semester-phase', '_semester_phase.html', False, ('academic_phase_supply_preparation', 'academic_term_snapshot_quality')),
    ('ops-academic-calendar', '_academic_calendar.html', True, ()),
    ('ops-mutual-aid', '_mutual_aid.html', True, ('mutual_aid_feedback_notes',)),
    ('ops-order-lifecycle', '_order_lifecycle.html', True, ()),
    ('ops-reminders', '_lifecycle_reminders.html', True, ('lifecycle_reminder_term_trend',)),
    ('ops-scheduler', '_scheduler.html', True, ('task_run_ledger_integrity',)),
    ('ops-borrow', '_borrow.html', True, ()),
    ('ops-demand-radar', '_demand_radar.html', True, ()),
    ('ops-meeting', '_meeting.html', False, ('campus_location_service_rule_quality', 'meeting_attendance', 'meeting_service_window_quality')),
    ('ops-handover-codes', '_handover_codes.html', False, ('handoff_code_usage', 'return_handshake')),
    ('ops-delivery-confirm', '_delivery_confirm.html', False, ('delivery_handshake', 'order_deadline_reminders', 'transaction_safety_acknowledgement_quality')),
    ('ops-saved-search', '_saved_search.html', False, ('first_viewed', 'saved_search_latency', 'saved_search_limit_shape', 'saved_search_quiet_window')),
    ('ops-lost-found', '_lost_found.html', False, (
        'lost_found_matched_outcome', 'lost_found_report_quality',
    )),
    ('ops-campus-verify', '_campus_verify.html', False, ('campus_verification_friction',)),
    ('ops-queues', '_free_claims.html', False, ('gift_application_queue',)),
    ('ops-item-images', '_item_images.html', False, ('item_image_coverage',)),
    ('ops-moderation', '_moderation_ops.html', True, ('moderation_appeal_quality', 'moderation_rule_quality')),
    ('ops-trade-quality', '_trade_quality.html', False, ('availability_watch_quality', 'dispute_resolution_quality', 'rating_quality')),
    ('ops-deal-outcomes', '_deal_outcomes.html', False, ('demand_response_conversion', 'lost_found_lead_quality', 'meeting_reschedule')),
    ('ops-item-refresh', '_item_refresh.html', False, ('item_refresh_event_ledger', 'refresh_quota_drift')),
    ('ops-search-exposure', '_search_exposure.html', False, ('search_exposure_cold_start', 'search_impression_pairing_quality')),
    ('ops-price-signal', '_price_signal.html', False, ('category_price_reference', 'price_change_quality', 'price_response', 'refresh_response', 'relist_freshness')),
    ('ops-contribution', '_contribution.html', False, ('contribution_milestone_coverage', 'contribution_milestone_phase_rhythm', 'contribution_tier_shape')),
    ('ops-audit-shapes', '_audit_shapes.html', False,
     ('borrow_days_shape', 'dispute_evidence_shape', 'profile_contact_shape',
      'moderation_risk_tier_shape')),
    ('ops-build-performance', '_build_performance.html', True, ALL_SHARED_PANELS),
    ('ops-locations', '_locations.html', False, ('location_maintenance_decision_review', 'location_maintenance_queue', 'location_registry_quality')),
)

# 锚点 -> 那一行。分区是顺序结构，查找却全是按键，所以两份都要留。
OPERATIONS_SECTION_FRAGMENT_MAP = {
    anchor: (partial, needs_core, panels)
    for anchor, partial, needs_core, panels in OPERATIONS_SECTION_FRAGMENTS
}


def section_anchors():
    """按页面顺序返回全部分区锚点。"""
    return tuple(anchor for anchor, _p, _c, _k in OPERATIONS_SECTION_FRAGMENTS)


def section_partial(anchor):
    """分区对应的 partial 文件名；锚点不存在时返回 None。"""
    row = OPERATIONS_SECTION_FRAGMENT_MAP.get(anchor)
    return row[0] if row else None


def section_needs_core(anchors):
    """这些分区里有没有一个要用到核心统计。

    核心统计整座看板只跑一次就够，所以是「或」而不是「与」：只要有一个分区
    要它，这一次请求就得跑。
    """
    for anchor in anchors:
        row = OPERATIONS_SECTION_FRAGMENT_MAP.get(anchor)
        if row and row[1]:
            return True
    return False


def section_panel_keys(anchors):
    """这些分区需要哪些共享面板的键。

    返回 None 表示「全部」——某个分区点名要整座看板时，剩下的一切筛选都不该做。
    """
    wanted = set()
    for anchor in anchors:
        row = OPERATIONS_SECTION_FRAGMENT_MAP.get(anchor)
        if not row:
            continue
        if row[2] == ALL_SHARED_PANELS:
            return None
        wanted.update(row[2])
    return wanted
