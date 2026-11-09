# -*- coding: utf-8 -*-
"""Cover the governance SLA aggregation and the workbench built on it.

The cases worth covering are the ones the module exists to answer. A case that
appears in both the period and the live backlog must be counted once; a case
opened before the period must still show up in the backlog even though it is
not part of the period's throughput; and the three deadlines must stay
different, because a shared deadline would either drown the incident queue in
false alarms or let reports rot.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from .models import (
    CampusLocation, Category, Item, LostFoundPost, LostFoundReport,
    MeetingAppointment, MeetingIncident, MeetingIncidentEvent, Order,
    OrderDispute, Report,
)
from .governance_sla import (
    SLA_TARGETS, SLA_WARNING_RATIO, build_governance_sla,
    normalise_case,
)


class NormaliseCaseTests(TestCase):
    """The shared shape the aggregation consumes."""

    def test_open_statuses_are_recognised_across_queues(self):
        # 三个队列对"未处理"用了不同的词汇，归一化后才能一起比较。
        for status in ('pending', 'reviewing', 'open'):
            case = normalise_case(
                kind='report', status=status, created_at=timezone.now(),
                closed_at=None, reviewer_id=None,
            )
            self.assertTrue(case['is_open'])

        for status in ('resolved', 'rejected', 'dismissed'):
            case = normalise_case(
                kind='dispute', status=status, created_at=timezone.now(),
                closed_at=timezone.now(), reviewer_id=1,
            )
            self.assertFalse(case['is_open'])

    def test_deadline_defaults_to_the_queue_target(self):
        case = normalise_case(
            kind='incident', status='open', created_at=timezone.now(),
            closed_at=None, reviewer_id=None,
        )
        self.assertEqual(case['sla_seconds'], SLA_TARGETS['incident'].total_seconds())
        self.assertEqual(case['sla_label'], '1 天')

    def test_deadline_can_be_overridden_per_case(self):
        case = normalise_case(
            kind='report', status='open', created_at=timezone.now(),
            closed_at=None, reviewer_id=None, target=timedelta(hours=6),
        )
        self.assertEqual(case['sla_label'], '6 小时')

    def test_extra_payload_passes_through_and_builds_a_stable_key(self):
        case = normalise_case(
            kind='report', status='pending', created_at=timezone.now(),
            closed_at=None, reviewer_id=None, case_id=7,
            subject='测试商品', counterparty='seller',
        )
        self.assertEqual(case['subject'], '测试商品')
        self.assertEqual(case['counterparty'], 'seller')
        self.assertEqual(case['case_key'], ('report', 7))


class GovernanceSlaTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='gov-staff', password='safe-password-123', is_staff=True,
        )
        self.member = User.objects.create_user(
            username='gov-member', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='gov-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='治理测试分类')
        self.location = CampusLocation.objects.create(name='治理测试地点')
        self.now = timezone.now()

    def make_item(self, seller=None, title='治理测试商品'):
        return Item.objects.create(
            title=title, description='用于测试治理时效统计。', price='30.00',
            category=self.category, location=self.location,
            condition='9成新', seller=seller or self.member,
        )

    def make_order(self, item=None, seller=None, buyer=None):
        item = item or self.make_item(seller=seller)
        return Order.objects.create(
            item=item, buyer=buyer or self.other, seller=item.seller,
            meeting_location=self.location, agreed_price='30.00',
            status='confirmed',
        )

    def make_report(self, *, status='pending', created_ago=timedelta(), closed_ago=None):
        report = Report.objects.create(
            item=self.make_item(), reporter=self.other, reason='spam',
            detail='需要处理。', status=status,
        )
        updates = {'created_at': self.now - created_ago}
        if closed_ago is not None:
            updates['status'] = 'resolved'
            updates['reviewed_at'] = self.now - closed_ago
            updates['reviewer_id'] = self.staff.pk
        Report.objects.filter(pk=report.pk).update(**updates)
        report.refresh_from_db()
        return report

    def make_lost_found_report(
        self, *, status='pending', created_ago=timedelta(), closed_ago=None,
        reason='impersonation',
    ):
        # 发布者和举报人必须是两个人：模型自己禁止举报自己的记录，这也正是
        # 这条队列存在的理由——失物招领是站内唯一要陌生人凭描述交出物品的地方。
        post = LostFoundPost.objects.create(
            reporter=self.member, post_type='lost', title='治理测试失物招领',
            description='用于测试治理时效统计。', occurred_at=self.now - timedelta(hours=2),
            expires_at=self.now + timedelta(days=5), status='active',
        )
        report = LostFoundReport.objects.create(
            post=post, reporter=self.other, reason=reason, detail='需要处理。',
            status=status,
        )
        updates = {'created_at': self.now - created_ago}
        if closed_ago is not None:
            updates['status'] = 'resolved'
            updates['reviewed_at'] = self.now - closed_ago
            updates['reviewer_id'] = self.staff.pk
        LostFoundReport.objects.filter(pk=report.pk).update(**updates)
        report.refresh_from_db()
        return report

    def make_dispute(self, *, status='open', created_ago=timedelta(), closed_ago=None):
        dispute = OrderDispute.objects.create(
            order=self.make_order(), opened_by=self.other, reason='not_received',
            detail='未收到商品。', status=status,
        )
        updates = {'created_at': self.now - created_ago}
        if closed_ago is not None:
            updates['status'] = 'resolved'
            updates['resolved_at'] = self.now - closed_ago
            updates['reviewer_id'] = self.staff.pk
        OrderDispute.objects.filter(pk=dispute.pk).update(**updates)
        dispute.refresh_from_db()
        return dispute

    def make_incident(self, *, status='open', created_ago=timedelta(), closed_ago=None):
        order = self.make_order()
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.member, location=self.location,
            start_at=self.now + timedelta(days=1),
            end_at=self.now + timedelta(days=1, hours=1), status='confirmed',
        )
        incident = MeetingIncident.objects.create(
            appointment=appointment, reported_by=self.other, accused=self.member,
            reason='no_show', detail='对方未到场。', status=status,
        )
        updates = {'created_at': self.now - created_ago}
        if closed_ago is not None:
            updates['status'] = 'resolved'
            updates['reviewed_at'] = self.now - closed_ago
            updates['reviewer_id'] = self.staff.pk
        MeetingIncident.objects.filter(pk=incident.pk).update(**updates)
        incident.refresh_from_db()
        return incident

    def add_incident_event(
        self, incident, event_type, previous_status, new_status, *,
        ago=timedelta(), note='',
    ):
        return MeetingIncidentEvent.objects.create(
            incident=incident,
            event_type=event_type,
            previous_status=previous_status,
            new_status=new_status,
            occurred_at=self.now - ago,
            note=note,
        )

    def add_complete_incident_chain(
        self, incident, *, terminal='resolved', reported_ago=timedelta(hours=8),
        started_ago=timedelta(hours=6), concluded_ago=timedelta(),
    ):
        self.add_incident_event(
            incident, 'reported', '', 'open', ago=reported_ago,
        )
        self.add_incident_event(
            incident, 'review_started', 'open', 'reviewing', ago=started_ago,
        )
        self.add_incident_event(
            incident, terminal, 'reviewing', terminal, ago=concluded_ago,
        )

    def test_empty_platform_reports_nothing_and_no_recommendations(self):
        data = build_governance_sla(days=30, now=self.now)

        self.assertFalse(data['has_data'])
        self.assertEqual(data['total_cases'], 0)
        self.assertEqual(data['total_open'], 0)
        self.assertEqual(data['total_overdue'], 0)
        self.assertIsNone(data['within_sla_rate'])
        self.assertIn('所有队列均已清空', data['summary'])
        self.assertEqual(data['recommendations'], [])

    def test_every_queue_is_reported_with_its_own_deadline(self):
        self.make_report()
        self.make_dispute()
        self.make_incident()
        self.make_lost_found_report()

        rows = {row['kind']: row for row in build_governance_sla(days=30, now=self.now)['queues']}

        # 借用催收队列只有真正升级到三级的订单才出现，这里没有任何借用单。
        self.assertEqual(
            set(rows),
            {'report', 'lost_found_report', 'dispute', 'incident', 'borrow'},
        )
        # 时限必须各不相同：预约异常阻塞一次线下交付，举报只是离线复核。
        self.assertEqual(rows['report']['sla_label'], '2 天')
        self.assertEqual(rows['lost_found_report']['sla_label'], '1 天')
        self.assertEqual(rows['dispute']['sla_label'], '3 天')
        self.assertEqual(rows['incident']['sla_label'], '1 天')
        self.assertEqual(rows['incident']['total'], 1)

    def test_lost_found_reports_are_tighter_than_item_reports(self):
        # 同一个小时数，两种举报的命运不一样：一条商品挂着，损失是一次难看的
        # 页面；一条失物招领挂着，可能已经有人拿着假身份把东西领走了。所以
        # 失物招领举报的时限必须是商品举报的一半，而不是借用一个数字。
        self.make_lost_found_report(created_ago=timedelta(hours=30))

        rows = {row['kind']: row for row in build_governance_sla(days=30, now=self.now)['queues']}

        self.assertEqual(rows['lost_found_report']['overdue'], 1)
        self.assertEqual(rows['report']['overdue'], 0)
        self.assertLess(
            rows['lost_found_report']['sla_seconds'], rows['report']['sla_seconds'],
        )

    def test_lost_found_report_latency_is_measured_to_the_review(self):
        # 举报的处理时刻是 reviewed_at：打开审核页不算响应，写进审核人才算。
        self.make_lost_found_report(
            created_ago=timedelta(hours=5), closed_ago=timedelta(hours=4, minutes=30),
        )

        rows = {
            row['kind']: row
            for row in build_governance_sla(days=30, now=self.now)['queues']
        }
        row = rows['lost_found_report']

        self.assertEqual(row['total'], 1)
        self.assertEqual(row['closed'], 1)
        self.assertEqual(row['open'], 0)
        self.assertEqual(row['median_seconds'], 30 * 60)
        self.assertEqual(row['median_label'], '30 分钟')
        self.assertEqual(row['within_sla_rate'], 100.0)

    def test_a_lost_found_report_opened_before_the_period_still_shows_up(self):
        # 积压不按统计周期过滤：周期前就提交、还没人看的举报恰恰是最该看见的。
        self.make_lost_found_report(created_ago=timedelta(days=10))

        data = build_governance_sla(days=1, now=self.now)

        self.assertFalse(data['has_data'])
        self.assertEqual(data['total_open'], 1)
        self.assertEqual(data['total_overdue'], 1)

    def test_lost_found_reports_are_listed_next_to_item_reports(self):
        # 两条举报通道挨着放，时限差一半才看得见；借用催收垫底，因为它的时限
        # 不是 moderation 目标，而是催收阶梯的尾巴。
        self.make_report()
        self.make_lost_found_report()

        kinds = [
            row['kind']
            for row in build_governance_sla(days=30, now=self.now)['queues']
        ]

        self.assertEqual(
            kinds, ['report', 'lost_found_report', 'dispute', 'incident', 'borrow'],
        )

    def test_closed_case_latency_is_measured_to_the_review_timestamp(self):
        self.make_report(created_ago=timedelta(hours=5), closed_ago=timedelta(hours=4, minutes=30))

        row = build_governance_sla(days=30, now=self.now)['queues'][0]

        self.assertEqual(row['total'], 1)
        self.assertEqual(row['closed'], 1)
        self.assertEqual(row['open'], 0)
        self.assertEqual(row['median_seconds'], 30 * 60)
        self.assertEqual(row['median_label'], '30 分钟')
        self.assertEqual(row['within_sla_rate'], 100.0)

    def test_case_counted_once_even_when_it_appears_in_both_windows(self):
        # 这件举报既在周期内、又是当前待处理事项，只能被计一次。
        self.make_report(created_ago=timedelta(hours=2))

        data = build_governance_sla(days=30, now=self.now)
        row = data['queues'][0]

        self.assertEqual(row['total'], 1)
        self.assertEqual(row['open'], 1)
        self.assertEqual(row['closed'], 0)
        self.assertEqual(data['total_cases'], 1)
        self.assertEqual(data['total_open'], 1)

    def test_backlog_is_not_limited_by_the_statistics_window(self):
        # 周期只有 1 天，这件举报开在 10 天前，不该被统计窗口藏起来。
        self.make_report(created_ago=timedelta(days=10))

        data = build_governance_sla(days=1, now=self.now)

        self.assertFalse(data['has_data'])
        self.assertEqual(data['total_open'], 1)
        self.assertEqual(data['total_overdue'], 1)
        self.assertIn('超过处理时限', data['summary'])

    def test_overdue_and_warning_are_classified_against_each_deadline(self):
        # 举报时限 2 天：一件刚开，一件过了 75% 警告线，一件已超期。
        self.make_report(created_ago=timedelta(hours=2))
        self.make_report(created_ago=timedelta(hours=40))
        self.make_report(created_ago=timedelta(days=3))

        row = build_governance_sla(days=30, now=self.now)['queues'][0]

        self.assertEqual(row['open'], 3)
        self.assertEqual(row['warning'], 1)
        self.assertEqual(row['overdue'], 1)
        self.assertEqual(row['overdue_rate'], round(1 / 3 * 100, 1))

    def test_warning_boundary_uses_the_configured_ratio(self):
        warning_at = SLA_TARGETS['dispute'] * SLA_WARNING_RATIO
        self.make_dispute(created_ago=warning_at - timedelta(minutes=1))
        self.make_dispute(created_ago=warning_at + timedelta(minutes=1))

        row = {
            queue['kind']: queue
            for queue in build_governance_sla(days=30, now=self.now)['queues']
        }['dispute']

        self.assertEqual(row['warning'], 1)
        self.assertEqual(row['overdue'], 0)

    def test_incident_overdue_is_flagged_faster_than_a_report(self):
        # 同一件开了 30 小时的事项：对举报来说还在时限内，对预约异常已经超期。
        self.make_report(created_ago=timedelta(hours=30))
        self.make_incident(created_ago=timedelta(hours=30))

        rows = {row['kind']: row for row in build_governance_sla(days=30, now=self.now)['queues']}

        self.assertEqual(rows['report']['overdue'], 0)
        self.assertEqual(rows['incident']['overdue'], 1)

    def test_closed_cases_never_appear_in_the_overdue_list(self):
        self.make_report(created_ago=timedelta(days=9), closed_ago=timedelta(days=8))

        data = build_governance_sla(days=30, now=self.now)

        self.assertEqual(data['total_overdue'], 0)
        self.assertEqual(data['overdue_cases'], [])
        self.assertIn('所有队列均已清空', data['summary'])

    def test_cases_outside_the_period_are_excluded_from_throughput(self):
        self.make_report(created_ago=timedelta(hours=2), closed_ago=timedelta(hours=1))
        self.make_report(created_ago=timedelta(days=40), closed_ago=timedelta(days=39))

        row = build_governance_sla(days=30, now=self.now)['queues'][0]

        self.assertEqual(row['total'], 1)
        self.assertEqual(row['closed'], 1)

    def test_platform_rate_counts_only_cases_handled_within_their_deadline(self):
        # 一件 30 分钟内处理完，一件拖了 5 天，只有第一件算"时限内完成"。
        self.make_report(created_ago=timedelta(hours=6), closed_ago=timedelta(minutes=30))
        self.make_dispute(created_ago=timedelta(days=10), closed_ago=timedelta(days=5))

        data = build_governance_sla(days=30, now=self.now)

        self.assertEqual(data['total_closed'], 2)
        self.assertEqual(data['within_sla_rate'], 50.0)

    def test_recommendations_name_the_overdue_queue_and_the_waiting_time(self):
        self.make_incident(created_ago=timedelta(days=2))

        recommendations = ' '.join(build_governance_sla(days=30, now=self.now)['recommendations'])

        self.assertIn('交付预约异常', recommendations)
        self.assertIn('已超过 1 天处理时限', recommendations)

    def test_healthy_platform_gets_a_reassuring_recommendation(self):
        self.make_report(created_ago=timedelta(hours=4), closed_ago=timedelta(hours=3))

        data = build_governance_sla(days=30, now=self.now)

        self.assertEqual(
            data['recommendations'],
            ['各治理队列都在处理时限内，暂时不需要调整审核人力。'],
        )

    def test_case_rows_carry_a_stable_key_and_the_age_so_far(self):
        self.make_report(created_ago=timedelta(hours=5))

        row = build_governance_sla(days=30, now=self.now)['queues'][0]
        case = row['cases'][0]

        self.assertEqual(case['case_key'][0], 'report')
        self.assertEqual(case['age_seconds'], 5 * 3600)
        self.assertEqual(case['age_label'], '5 小时')
        self.assertFalse(case['is_overdue'])
        self.assertGreater(case['remaining_seconds'], 0)


    def test_event_timing_uses_the_two_real_state_transition_segments(self):
        incident = self.make_incident(
            status='resolved', created_ago=timedelta(hours=10),
            closed_ago=timedelta(),
        )
        self.add_complete_incident_chain(incident)
        self.add_incident_event(
            incident, 'note_updated', 'reviewing', 'reviewing',
            ago=timedelta(hours=3), note='补充说明',
        )

        data = build_governance_sla(days=30, now=self.now)
        timing = data['incident_timing']
        case = next(
            row for row in data['queues'][3]['cases']
            if row['case_id'] == incident.pk
        )

        self.assertEqual(timing['sample_count'], 1)
        self.assertEqual(timing['response']['measurable_count'], 1)
        self.assertEqual(timing['response']['median_seconds'], 2 * 3600)
        self.assertEqual(timing['response']['p90_seconds'], 2 * 3600)
        self.assertEqual(timing['conclusion']['measurable_count'], 1)
        self.assertEqual(timing['conclusion']['median_seconds'], 6 * 3600)
        self.assertEqual(timing['conclusion']['p90_seconds'], 6 * 3600)
        self.assertEqual(timing['historical_unknown_count'], 0)
        self.assertEqual(timing['invalid_chain_count'], 0)
        self.assertEqual(case['incident_timing_state'], 'valid')
        self.assertEqual(case['incident_response_label'], '首次响应 2 小时')
        self.assertEqual(case['incident_conclusion_label'], '开始后结论 6 小时')
        self.assertEqual(len(case['incident_events']), 4)

    def test_note_snapshot_must_match_the_state_at_that_point(self):
        incident = self.make_incident(
            status='resolved', created_ago=timedelta(hours=10),
            closed_ago=timedelta(),
        )
        self.add_complete_incident_chain(incident)
        self.add_incident_event(
            incident, 'note_updated', 'open', 'reviewing',
            ago=timedelta(minutes=30), note='错误的状态快照',
        )

        timing = build_governance_sla(days=30, now=self.now)['incident_timing']

        self.assertEqual(timing['invalid_chain_count'], 1)
        self.assertEqual(timing['response']['measurable_count'], 0)
        self.assertEqual(timing['conclusion']['measurable_count'], 0)
        self.assertEqual(timing['conclusion']['unknown_count'], 1)

    def test_legacy_incident_without_events_is_historical_unknown_not_zero(self):
        self.make_incident(
            status='resolved', created_ago=timedelta(hours=4),
            closed_ago=timedelta(hours=1),
        )

        timing = build_governance_sla(days=30, now=self.now)['incident_timing']

        self.assertEqual(timing['historical_unknown_count'], 1)
        self.assertEqual(timing['invalid_chain_count'], 0)
        self.assertEqual(timing['response']['measurable_count'], 0)
        self.assertIsNone(timing['response']['median_seconds'])
        self.assertEqual(timing['response']['unknown_count'], 1)
        self.assertEqual(timing['conclusion']['unknown_count'], 1)
        self.assertEqual(timing['conclusion']['pending_count'], 0)

    def test_reported_only_open_incident_is_pending_instead_of_invalid(self):
        incident = self.make_incident(status='open')
        self.add_incident_event(
            incident, 'reported', '', 'open', ago=timedelta(hours=2),
        )

        timing = build_governance_sla(days=30, now=self.now)['incident_timing']

        self.assertEqual(timing['invalid_chain_count'], 0)
        self.assertEqual(timing['response']['pending_count'], 1)
        self.assertEqual(timing['response']['unknown_count'], 0)
        self.assertEqual(timing['conclusion']['pending_count'], 1)
        self.assertEqual(timing['conclusion']['unknown_count'], 0)

    def test_missing_reported_event_invalidates_the_chain(self):
        incident = self.make_incident(status='reviewing')
        self.add_incident_event(
            incident, 'review_started', 'open', 'reviewing',
            ago=timedelta(hours=1),
        )

        timing = build_governance_sla(days=30, now=self.now)['incident_timing']

        self.assertEqual(timing['invalid_chain_count'], 1)
        self.assertEqual(timing['response']['measurable_count'], 0)
        self.assertEqual(timing['response']['unknown_count'], 1)
        self.assertEqual(timing['conclusion']['pending_count'], 1)

    def test_missing_review_started_event_invalidates_a_closed_chain(self):
        incident = self.make_incident(
            status='resolved', closed_ago=timedelta(hours=1),
        )
        self.add_incident_event(
            incident, 'reported', '', 'open', ago=timedelta(hours=4),
        )
        self.add_incident_event(
            incident, 'resolved', 'reviewing', 'resolved',
            ago=timedelta(hours=1),
        )

        timing = build_governance_sla(days=30, now=self.now)['incident_timing']

        self.assertEqual(timing['invalid_chain_count'], 1)
        self.assertEqual(timing['response']['unknown_count'], 1)
        self.assertEqual(timing['conclusion']['unknown_count'], 1)

    def test_duplicate_state_event_invalidates_the_chain(self):
        incident = self.make_incident(status='reviewing')
        self.add_incident_event(
            incident, 'reported', '', 'open', ago=timedelta(hours=4),
        )
        self.add_incident_event(
            incident, 'review_started', 'open', 'reviewing',
            ago=timedelta(hours=2),
        )
        self.add_incident_event(
            incident, 'review_started', 'open', 'reviewing',
            ago=timedelta(hours=1),
        )

        timing = build_governance_sla(days=30, now=self.now)['incident_timing']

        self.assertEqual(timing['invalid_chain_count'], 1)
        self.assertEqual(timing['response']['measurable_count'], 0)

    def test_out_of_order_state_events_invalidate_the_chain(self):
        incident = self.make_incident(
            status='resolved', closed_ago=timedelta(),
        )
        self.add_incident_event(
            incident, 'reported', '', 'open', ago=timedelta(hours=8),
        )
        self.add_incident_event(
            incident, 'review_started', 'open', 'reviewing',
            ago=timedelta(hours=10),
        )
        self.add_incident_event(
            incident, 'resolved', 'reviewing', 'resolved', ago=timedelta(),
        )

        timing = build_governance_sla(days=30, now=self.now)['incident_timing']

        self.assertEqual(timing['invalid_chain_count'], 1)
        self.assertEqual(timing['response']['measurable_count'], 0)

    def test_wrong_state_snapshot_invalidates_the_chain(self):
        incident = self.make_incident(status='reviewing')
        self.add_incident_event(
            incident, 'reported', '', 'open', ago=timedelta(hours=4),
        )
        self.add_incident_event(
            incident, 'review_started', '', 'reviewing',
            ago=timedelta(hours=2),
        )

        timing = build_governance_sla(days=30, now=self.now)['incident_timing']

        self.assertEqual(timing['invalid_chain_count'], 1)
        self.assertEqual(timing['response']['unknown_count'], 1)

    def test_unknown_cases_do_not_change_a_known_case_median(self):
        known = self.make_incident(
            status='resolved', created_ago=timedelta(hours=10),
            closed_ago=timedelta(),
        )
        self.add_complete_incident_chain(known)
        self.make_incident(
            status='resolved', created_ago=timedelta(hours=4),
            closed_ago=timedelta(hours=1),
        )

        timing = build_governance_sla(days=30, now=self.now)['incident_timing']

        self.assertEqual(timing['sample_count'], 2)
        self.assertEqual(timing['historical_unknown_count'], 1)
        self.assertEqual(timing['response']['measurable_count'], 1)
        self.assertEqual(timing['response']['median_seconds'], 2 * 3600)
        self.assertEqual(timing['conclusion']['measurable_count'], 1)
        self.assertEqual(timing['conclusion']['median_seconds'], 6 * 3600)

    def test_backlog_events_are_reused_but_not_added_to_the_period_denominator(self):
        incident = self.make_incident(
            status='reviewing', created_ago=timedelta(days=40),
        )
        self.add_incident_event(
            incident, 'reported', '', 'open', ago=timedelta(days=39),
        )
        self.add_incident_event(
            incident, 'review_started', 'open', 'reviewing',
            ago=timedelta(days=38),
        )

        data = build_governance_sla(days=30, now=self.now)
        timing = data['incident_timing']
        case = next(
            row for row in data['queues'][3]['cases']
            if row['case_id'] == incident.pk
        )

        self.assertEqual(timing['sample_count'], 0)
        self.assertEqual(timing['response']['measurable_count'], 0)
        self.assertEqual(case['incident_timing_state'], 'valid')
        self.assertEqual(case['incident_response_label'], '首次响应 1 天')
        self.assertEqual(len(case['incident_events']), 2)

    def test_incident_events_are_loaded_once_per_sla_build(self):
        incident = self.make_incident(
            status='resolved', closed_ago=timedelta(),
        )
        self.add_complete_incident_chain(incident)

        with CaptureQueriesContext(connection) as captured:
            build_governance_sla(days=30, now=self.now)

        event_queries = [
            query for query in captured.captured_queries
            if 'meetingincidentevent' in query['sql'].lower()
        ]
        self.assertEqual(len(event_queries), 1)


class GovernanceWorkbenchTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='workbench-staff', password='safe-password-123', is_staff=True,
        )
        self.member = User.objects.create_user(
            username='workbench-member', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='workbench-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='工作台测试分类')
        self.location = CampusLocation.objects.create(name='工作台测试地点')
        self.now = timezone.now()

    def make_item(self):
        return Item.objects.create(
            title='工作台测试商品', description='用于测试治理工作台。', price='25.00',
            category=self.category, location=self.location,
            condition='9成新', seller=self.member,
        )

    def make_order(self):
        item = self.make_item()
        return Order.objects.create(
            item=item, buyer=self.other, seller=item.seller,
            meeting_location=self.location, agreed_price='25.00', status='confirmed',
        )

    def make_report(self):
        return Report.objects.create(
            item=self.make_item(), reporter=self.other, reason='scam',
            detail='疑似诈骗。',
        )

    def make_incident(self):
        order = self.make_order()
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.member, location=self.location,
            start_at=self.now + timedelta(days=1),
            end_at=self.now + timedelta(days=1, hours=1), status='confirmed',
        )
        return MeetingIncident.objects.create(
            appointment=appointment, reported_by=self.other, accused=self.member,
            reason='safety', detail='现场安全问题。',
        )

    def test_workbench_requires_staff(self):
        self.client.login(username='workbench-member', password='safe-password-123')

        response = self.client.get(reverse('governance_workbench'))

        self.assertEqual(response.status_code, 403)

    def test_workbench_lists_open_cases_from_every_queue(self):
        self.make_report()
        self.make_incident()

        self.client.login(username='workbench-staff', password='safe-password-123')
        response = self.client.get(reverse('governance_workbench'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['cases']), 2)
        self.assertContains(response, '工作台测试商品')
        self.assertContains(response, '现场安全问题')

    def test_queue_filter_narrows_to_one_queue(self):
        self.make_report()
        self.make_incident()

        self.client.login(username='workbench-staff', password='safe-password-123')
        response = self.client.get(reverse('governance_workbench'), {'kind': 'incident'})

        self.assertEqual(len(response.context['cases']), 1)
        self.assertEqual(response.context['cases'][0]['kind'], 'incident')

    def test_status_filter_separates_open_from_closed(self):
        self.make_report()
        self.make_incident()
        MeetingIncident.objects.update(status='dismissed')

        self.client.login(username='workbench-staff', password='safe-password-123')
        response = self.client.get(reverse('governance_workbench'), {'status': 'closed'})

        self.assertEqual(len(response.context['cases']), 1)
        self.assertFalse(response.context['cases'][0]['is_open'])

    def test_unknown_filter_values_fall_back_to_the_defaults(self):
        self.make_report()

        self.client.login(username='workbench-staff', password='safe-password-123')
        response = self.client.get(
            reverse('governance_workbench'), {'kind': 'nope', 'status': 'nope'},
        )

        self.assertEqual(response.context['kind_filter'], 'all')
        self.assertEqual(response.context['status_filter'], 'open')

    def test_period_selector_is_carried_into_the_statistics(self):
        self.client.login(username='workbench-staff', password='safe-password-123')

        response = self.client.get(reverse('governance_workbench'), {'days': '90'})

        self.assertEqual(response.context['days'], 90)


    def test_workbench_labels_a_legacy_incident_as_historically_unknown(self):
        self.make_incident()
        self.client.login(username='workbench-staff', password='safe-password-123')

        response = self.client.get(
            reverse('governance_workbench'), {'kind': 'incident'},
        )

        self.assertEqual(
            response.context['cases'][0]['incident_timing_state'],
            'historical_unknown',
        )
        self.assertContains(response, '预约异常事件时效')
        self.assertContains(response, '首次响应 · 历史不可知')
        self.assertContains(response, '历史不可知 1 条')


class IncidentReviewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='incident-staff', password='safe-password-123', is_staff=True,
        )
        self.member = User.objects.create_user(
            username='incident-member', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='incident-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='异常测试分类')
        self.location = CampusLocation.objects.create(name='异常测试地点')
        self.item = Item.objects.create(
            title='异常测试商品', description='用于测试预约异常处理。', price='25.00',
            category=self.category, location=self.location,
            condition='9成新', seller=self.member,
        )
        self.order = Order.objects.create(
            item=self.item, buyer=self.other, seller=self.member,
            meeting_location=self.location, agreed_price='25.00', status='confirmed',
        )
        self.appointment = MeetingAppointment.objects.create(
            order=self.order, proposed_by=self.member, location=self.location,
            start_at=timezone.now() + timedelta(days=1),
            end_at=timezone.now() + timedelta(days=1, hours=1), status='confirmed',
        )
        self.incident = MeetingIncident.objects.create(
            appointment=self.appointment, reported_by=self.other, accused=self.member,
            reason='no_show', detail='对方未到场。',
        )

    def test_review_page_requires_staff(self):
        self.client.login(username='incident-member', password='safe-password-123')

        response = self.client.get(
            reverse('review_meeting_incident', args=[self.incident.id]),
        )

        self.assertEqual(response.status_code, 403)

    def test_form_only_offers_closing_outcomes(self):
        # 前台只下结论，不提供"转处理中"这种中间态，避免和另外两个队列不一致。
        from .forms import MeetingIncidentReviewForm

        form = MeetingIncidentReviewForm(instance=self.incident)
        values = {value for value, _label in form.fields['status'].choices}

        self.assertEqual(values, {'resolved', 'dismissed'})

    def test_reviewing_a_case_closes_it_notifies_both_sides_and_logs_the_order(self):
        self.client.login(username='incident-staff', password='safe-password-123')

        response = self.client.post(
            reverse('review_meeting_incident', args=[self.incident.id]),
            {'status': 'resolved', 'resolution_note': '已核实到场记录。'},
        )

        self.assertRedirects(response, reverse('governance_workbench'))
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, 'resolved')
        self.assertEqual(self.incident.reviewer, self.staff)
        self.assertIsNotNone(self.incident.reviewed_at)
        self.assertEqual(self.incident.resolution_note, '已核实到场记录。')
        # 结论要同时到达双方，并留在订单时间线上可追溯。
        self.assertEqual(self.incident.reported_by.notifications.count(), 1)
        self.assertEqual(self.incident.accused.notifications.count(), 1)
        self.assertTrue(self.order.events.filter(note__icontains='已确认异常').exists())

    def test_an_already_closed_case_is_not_reviewed_twice(self):
        MeetingIncident.objects.filter(pk=self.incident.pk).update(status='dismissed')
        self.client.login(username='incident-staff', password='safe-password-123')

        response = self.client.post(
            reverse('review_meeting_incident', args=[self.incident.id]),
            {'status': 'resolved', 'resolution_note': '重复处理。'},
        )

        self.assertRedirects(response, reverse('governance_workbench'))
        self.incident.refresh_from_db()
        self.assertEqual(self.incident.status, 'dismissed')
        self.assertEqual(self.incident.reviewer, None)

    def test_closed_case_is_no_longer_listed_as_open(self):
        MeetingIncident.objects.filter(pk=self.incident.pk).update(status='dismissed')
        self.client.login(username='incident-staff', password='safe-password-123')

        response = self.client.get(reverse('governance_workbench'))

        self.assertEqual(response.context['cases'], [])


class GovernanceDashboardTests(TestCase):
    """The dashboard panel and the alert that points at it."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='dash-staff', password='safe-password-123', is_staff=True,
        )
        self.seller = User.objects.create_user(
            username='dash-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='dash-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='看板治理分类')
        self.location = CampusLocation.objects.create(name='看板治理地点')

    def make_overdue_incident(self):
        item = Item.objects.create(
            title='看板治理商品', description='用于测试看板治理面板。', price='30.00',
            category=self.category, location=self.location,
            condition='9成新', seller=self.seller,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
        )
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.seller, location=self.location,
            start_at=timezone.now() + timedelta(days=1),
            end_at=timezone.now() + timedelta(days=1, hours=1), status='confirmed',
        )
        incident = MeetingIncident.objects.create(
            appointment=appointment, reported_by=self.buyer, accused=self.seller,
            reason='no_show', detail='对方未到场。',
        )
        # 预约异常时限 24 小时，这里直接把创建时间推到 3 天前。
        MeetingIncident.objects.filter(pk=incident.pk).update(
            created_at=timezone.now() - timedelta(days=3),
        )
        return incident

    def test_dashboard_renders_the_governance_panel(self):
        self.client.login(username='dash-staff', password='safe-password-123')

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '治理队列处理时效')
        self.assertContains(response, '时限内完成率')
        self.assertContains(response, '进入治理工作台')
        self.assertContains(response, '预约异常事件时效')
        self.assertContains(response, '首次响应')
        self.assertContains(response, '开始后结论')

    def test_dashboard_keeps_a_zero_percent_rate_instead_of_rendering_a_dash(self):
        incident = self.make_overdue_incident()
        now = timezone.now()
        MeetingIncidentEvent.objects.create(
            incident=incident, event_type='reported', previous_status='',
            new_status='open', occurred_at=now - timedelta(hours=30),
        )
        MeetingIncidentEvent.objects.create(
            incident=incident, event_type='review_started', previous_status='open',
            new_status='reviewing', occurred_at=now - timedelta(hours=1),
        )
        MeetingIncident.objects.filter(pk=incident.pk).update(status='reviewing')
        self.client.login(username='dash-staff', password='safe-password-123')

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        timing = response.context['governance_sla']['incident_timing']
        self.assertEqual(timing['response']['within_24h_rate'], 0.0)
        self.assertContains(response, '0 / 0.0%')

    def test_overdue_incident_raises_a_critical_alert_linking_to_the_workbench(self):
        self.make_overdue_incident()
        self.client.login(username='dash-staff', password='safe-password-123')

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        alerts = {alert['key']: alert for alert in response.context['operational_alerts']}
        self.assertIn('governance_overdue_incident', alerts)
        # 预约异常阻塞一次线下交付，必须以最高优先级出现。
        self.assertEqual(alerts['governance_overdue_incident']['severity'], 'critical')
        self.assertEqual(alerts['governance_overdue_incident']['action_url_name'], 'governance_workbench')
        self.assertContains(response, '处理超期')

    def test_healthy_queues_raise_no_governance_alert(self):
        self.client.login(username='dash-staff', password='safe-password-123')

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        alert_keys = {alert['key'] for alert in response.context['operational_alerts']}
        self.assertFalse([key for key in alert_keys if key.startswith('governance_')])

class GovernanceExportTests(TestCase):
    """导出要和面板说同一件事，否则下载回去的表会被读成另一个项目的数据。"""

    def setUp(self):
        self.staff = User.objects.create_user(
            username='sla-export-staff', password='safe-password-123', is_staff=True,
        )
        self.seller = User.objects.create_user(
            username='sla-export-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='sla-export-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='导出治理分类')
        self.location = CampusLocation.objects.create(name='导出治理地点')

    def make_overdue_incident(self):
        item = Item.objects.create(
            title='导出治理商品', description='用于测试治理导出。', price='30.00',
            category=self.category, location=self.location,
            condition='9成新', seller=self.seller,
        )
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=self.seller,
            meeting_location=self.location, agreed_price='30.00', status='confirmed',
        )
        appointment = MeetingAppointment.objects.create(
            order=order, proposed_by=self.seller, location=self.location,
            start_at=timezone.now() + timedelta(days=1),
            end_at=timezone.now() + timedelta(days=1, hours=1), status='confirmed',
        )
        incident = MeetingIncident.objects.create(
            appointment=appointment, reported_by=self.buyer, accused=self.seller,
            reason='no_show', detail='对方未到场。',
        )
        # 预约异常时限 24 小时，这里直接把创建时间推到 3 天前。
        MeetingIncident.objects.filter(pk=incident.pk).update(
            created_at=timezone.now() - timedelta(days=3),
        )
        return incident

    def test_the_export_carries_every_queue_and_the_deadline_it_used(self):
        # 四个队列都要在，且每条时限要随表导出：时限不同是这块的全部意义，
        # 下载回去的表若不带时限，就看不出哪一条是真的超期。
        self.make_overdue_incident()
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        text = response.content.decode('utf-8-sig')
        self.assertIn('治理队列处理时效', text)
        for label in ('商品举报', '交易争议', '交付预约异常', '借用逾期催收'):
            self.assertIn(label, text)
        # 1 天是预约异常的时限（_format_duration 按最粗的可读单位渲染），
        # 超过它才叫超期；待处理清单里那一条要真的标成已超期。
        self.assertIn('1 天', text)
        self.assertIn('已超期', text)

    def test_the_export_carries_the_two_event_phases_and_unknown_counts(self):
        self.make_overdue_incident()
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        text = response.content.decode('utf-8-sig')
        self.assertIn('预约异常事件时效', text)
        self.assertIn('首次响应：提交异常 → 开始处理', text)
        self.assertIn('开始后结论：开始处理 → 确认或驳回', text)
        self.assertIn('历史不可知', text)
        self.assertIn('事件链无法判定', text)
        self.assertIn('两类都不按零秒进入耗时分母', text)

    def test_the_export_states_that_it_only_reports(self):
        # 边界要随表导出：治理面板最容易被读成"可以自动派单"的授权，
        # 这句话必须在下载回去的表里，而不是只留在看板上。
        self.make_overdue_incident()
        self.client.force_login(self.staff)

        response = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        text = response.content.decode('utf-8-sig')
        self.assertIn('此处只报告', text)
        self.assertIn('不自动派单', text)
        self.assertIn('打开队列页不算响应', text)
