# -*- coding: utf-8 -*-
"""失物招领确认匹配之后的终局分布，测试。

这个模块读的量都带一个共同的坑：``matched`` 是一个单向门，当前代码里
``closed`` / ``expired`` / ``active`` 三档都到不了。测试要守住的是「照实判断」——
不预设哪一档一定有数据，也不因为预期是空就不报那一档。代码变了，看板要能自己跟上。

另一个坑是 ``matched_post`` 的互填。``review_lost_found_lead`` 的 accept 分支会把
关联记录也置为 ``matched`` 并互填 ``matched_post``，但测试造数据时如果只填一边，
``_build_counterpart_stats`` 就会把它判成单边匹配——那是造数据不完整，不是计算错误。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import (
    Category, CampusLocation, LostFoundLead, LostFoundPost, MutualAidFeedback,
)
from .lost_found_matched_outcome import (
    LIST_LIMIT, MIN_BUCKET_SAMPLE, MIN_MATCHED_POSTS, OVERDUE_BUCKETS,
    build_lost_found_matched_outcome,
)

# 参照点统一走 testing_clock：本地时间落到 23:59:00-24:00:00 时，各模块自己写的
# 23:59:00 会早于夹具的 auto_now_add 时间戳，把周期内记录整批排除。
from .testing_clock import local_tonight


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_post(reporter, *, post_type='lost', status='active', expires_at=None,
              matched_post=None, title='测试失物招领记录'):
    return LostFoundPost.objects.create(
        reporter=reporter,
        post_type=post_type,
        title=title,
        description='用于测试匹配终局的记录',
        occurred_at=timezone.now() - timedelta(days=2),
        expires_at=expires_at,
        status=status,
        matched_post=matched_post,
    )


def make_lead(post, respondent, *, status='accepted', related_post=None):
    return LostFoundLead.objects.create(
        post=post,
        respondent=respondent,
        related_post=related_post,
        message='我在图书馆三楼捡到一条校园卡',
        status=status,
    )


def make_feedback(lead, *, outcome='completed'):
    return MutualAidFeedback.objects.create(
        lost_found_lead=lead,
        submitted_by=lead.post.reporter,
        outcome=outcome,
        tags=['on_time'],
        note='东西找回来了',
    )


class LostFoundMatchedOutcomeTests(TestCase):
    def setUp(self):
        self.reporter = make_user('outcome-reporter')
        self.helper = make_user('outcome-helper')
        self.category = Category.objects.create(name='匹配终局测试分类')
        self.location = CampusLocation.objects.create(name='匹配终局测试地点')
        self.now = local_tonight()

    # ---------------------------------------------------------------- 数据形状

    def test_no_matched_posts_reports_no_data(self):
        """一条匹配记录都没有时 has_data 为假，看板走空状态而不是报 0%。"""
        make_post(self.reporter, status='active')

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(result['matched_total'], 0)

    def test_matched_post_is_counted_as_matched(self):
        """一条 status == 'matched' 的记录要进来，归「仍标记已匹配」。"""
        make_post(self.reporter, status='matched')

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        self.assertTrue(result['has_data'])
        self.assertEqual(result['matched_total'], 1)
        still = next(r for r in result['outcome_rows'] if r['key'] == 'still_matched')
        self.assertEqual(still['post_count'], 1)

    def test_post_that_left_matched_is_still_counted(self):
        """历史上匹配过、后来状态变了的记录也要进来——只能通过 matched_post 反查。"""
        post = make_post(self.reporter, status='closed', matched_post=None)
        # 造一条指向它的记录，让 post 参与过一次匹配，但它自己没填 matched_post。
        other = make_post(
            self.helper, post_type='found', status='matched', matched_post=post,
        )

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        self.assertEqual(result['matched_total'], 2)
        closed = next(r for r in result['outcome_rows'] if r['key'] == 'closed')
        self.assertEqual(closed['post_count'], 1)
        self.assertEqual(other.status, 'matched')

    # ---------------------------------------------------------------- 四种终局

    def test_four_outcomes_are_reported_even_when_three_are_empty(self):
        """四种终局都报，其中三档预期是空的——一栏永远是 0 的状态比注释更难被忽略。"""
        make_post(self.reporter, status='matched')

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        keys = [row['key'] for row in result['outcome_rows']]
        self.assertEqual(
            keys, ['still_matched', 'closed', 'expired', 'reopened'],
        )
        # 空档不是「删掉不管」：一栏永远是 0 的状态，比一段注释更难被忽略。
        for key in ('closed', 'expired', 'reopened'):
            row = next(r for r in result['outcome_rows'] if r['key'] == key)
            self.assertEqual(row['post_count'], 0)
            # 占比的分母是全部匹配记录，所以空档报 0.0；None 只属于「一条匹配记录
            # 都没有」那种连分母都不存在的情形。
            self.assertEqual(row['share'], 0.0)

    def test_outcome_share_denominator_is_all_matched_posts(self):
        """占比的分母是全部匹配记录，不是这一档自己。"""
        for _ in range(3):
            make_post(self.reporter, status='matched')
        # 这条 closed 要真的参与过一次匹配才进分母：自己填 matched_post，或者被
        # 别人指向。造一条没参与过匹配的 closed 记录，它本来就不该被算进来。
        counterpart = make_post(self.helper, post_type='found', status='closed')
        make_post(self.reporter, status='closed', matched_post=counterpart)

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        # 分母 5：3 条 matched、1 条 closed、以及被它指向的那条 counterpart——
        # counterpart 自己没填 matched_post，但被 closed 指向，同样参与过一次匹配。
        self.assertEqual(result['matched_total'], 5)
        still = next(r for r in result['outcome_rows'] if r['key'] == 'still_matched')
        self.assertEqual(still['post_count'], 3)
        self.assertEqual(still['share'], 60.0)
        closed = next(r for r in result['outcome_rows'] if r['key'] == 'closed')
        self.assertEqual(closed['post_count'], 2)
        self.assertEqual(closed['share'], 40.0)

    def test_outcome_below_threshold_is_flagged_not_hidden(self):
        """一档不足门槛时标记 small_sample，但不从表里消失。"""
        make_post(self.reporter, status='matched')

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        still = next(r for r in result['outcome_rows'] if r['key'] == 'still_matched')
        self.assertTrue(still['is_small_sample'])
        self.assertEqual(still['post_count'], 1)
        self.assertEqual(result['min_bucket_sample'], MIN_BUCKET_SAMPLE)

    # ---------------------------------------------------------------- 过期多久

    def test_overdue_stats_only_cover_still_matched(self):
        """过期多久的分母是「仍标记已匹配」这一档，不是全部匹配记录。"""
        make_post(
            self.reporter, status='matched',
            expires_at=self.now - timedelta(days=10),
        )
        make_post(
            self.reporter, status='closed',
            expires_at=self.now - timedelta(days=20),
        )

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        stats = result['overdue_stats']
        self.assertEqual(stats['measured_count'], 1)
        self.assertEqual(stats['oldest_label'], '10 天')

    def test_posts_without_deadline_are_counted_separately(self):
        """没有截止时间的记录不计入过期分布，单独数出来——那是右删失样本。"""
        make_post(self.reporter, status='matched', expires_at=None)
        make_post(
            self.reporter, status='matched',
            expires_at=self.now - timedelta(days=3),
        )

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        stats = result['overdue_stats']
        self.assertEqual(stats['measured_count'], 1)
        self.assertEqual(stats['no_deadline_count'], 1)

    def test_overdue_buckets_are_mutually_exclusive(self):
        """三个过期档互斥且完整：档位加起来等于分母。"""
        for days in (2, 10, 40):
            make_post(
                self.reporter, status='matched',
                expires_at=self.now - timedelta(days=days),
            )

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        stats = result['overdue_stats']
        counts = [row['count'] for row in stats['rows']]
        self.assertEqual(counts, [1, 1, 1])
        self.assertEqual(sum(counts), stats['measured_count'])
        self.assertEqual([row['label'] for row in stats['rows']],
                         [label for _limit, label in OVERDUE_BUCKETS])

    def test_overdue_rate_is_none_without_overdue_posts(self):
        """没有截止时间已过的记录时报 None，不报 0。"""
        make_post(
            self.reporter, status='matched',
            expires_at=self.now + timedelta(days=5),
        )

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        stats = result['overdue_stats']
        self.assertEqual(stats['measured_count'], 0)
        self.assertIsNone(stats['rows'][0]['share'])

    # ---------------------------------------------------------------- 双向匹配

    def test_mutual_match_is_recognised(self):
        """两边互填 matched_post 且都停在 matched，归互为匹配。"""
        a = make_post(self.reporter, post_type='lost', status='matched')
        b = make_post(
            self.helper, post_type='found', status='matched', matched_post=a,
        )
        a.matched_post = b
        a.save(update_fields=['matched_post'])

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        stats = result['counterpart_stats']
        self.assertEqual(stats['measured_count'], 2)
        self.assertEqual(stats['mutual_count'], 2)
        self.assertEqual(stats['one_sided_count'], 0)

    def test_one_sided_match_is_recognised(self):
        """只有一边填了 matched_post，归单边匹配——那不是 bug，用户可能删了自己那条。"""
        a = make_post(self.reporter, post_type='lost', status='matched')
        b = make_post(
            self.helper, post_type='found', status='matched', matched_post=a,
        )
        # a 没有回填 matched_post。双向性只数「认这笔账」的那一侧：b 填了，
        # a 没填，所以 b 是单边、a 连 counterpart 都没有。
        self.assertIsNone(a.matched_post_id)
        self.assertEqual(b.matched_post_id, a.pk)

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        stats = result['counterpart_stats']
        self.assertEqual(stats['measured_count'], 1)
        self.assertEqual(stats['one_sided_count'], 1)
        self.assertEqual(stats['mutual_count'], 0)
        self.assertEqual(stats['no_counterpart_count'], 1)

    def test_no_counterpart_is_counted_separately(self):
        """没有关联记录的匹配记录单独数，不算进双向性的分母。"""
        make_post(self.reporter, status='matched')

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        stats = result['counterpart_stats']
        self.assertEqual(stats['no_counterpart_count'], 1)
        self.assertEqual(stats['measured_count'], 0)
        self.assertIsNone(stats['mutual_rate'])

    def test_counterpart_stats_never_leak_the_other_post(self):
        """双向性只报计数，不报另一条记录的标题或发布者。"""
        a = make_post(self.reporter, post_type='lost', status='matched',
                      title='丢失身份证 330103199901011234')
        b = make_post(
            self.helper, post_type='found', status='matched',
            matched_post=a, title='捡到一张身份证',
        )
        a.matched_post = b
        a.save(update_fields=['matched_post'])

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        self.assertNotIn('身份证', repr(result['counterpart_stats']))
        self.assertNotIn(self.reporter.username, repr(result['counterpart_stats']))
        self.assertNotIn(self.helper.username, repr(result['counterpart_stats']))

    # ---------------------------------------------------------------- 反馈

    def test_feedback_rate_is_split_by_outcome(self):
        """反馈提交率按终局拆：仍标记已匹配的，和其余终局的，各自报。"""
        still = make_post(self.reporter, status='matched')
        make_lead(still, self.helper)
        # closed 那条要真的参与过一次匹配才进分母：自己填 matched_post，或者被
        # 别人指向。造一条没参与过匹配的 closed 记录，它本来就不该被算进来。
        counterpart = make_post(self.helper, post_type='found', status='matched')
        closed = make_post(self.reporter, status='closed', matched_post=counterpart)
        make_lead(closed, self.helper)
        make_feedback(LostFoundLead.objects.get(post=closed))

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        rows = {row['key']: row for row in result['feedback']['rows']}
        # 分母是 1、分子是 0，所以这里是 0.0 而不是 None：None 只属于空分母。
        self.assertEqual(rows['still_matched']['accepted_count'], 1)
        self.assertEqual(rows['still_matched']['feedback_count'], 0)
        self.assertEqual(rows['still_matched']['feedback_rate'], 0.0)
        self.assertEqual(rows['closed']['accepted_count'], 1)
        self.assertEqual(rows['closed']['feedback_count'], 1)
        self.assertEqual(rows['closed']['feedback_rate'], 100.0)

    def test_posts_without_accepted_lead_are_not_in_feedback_denominator(self):
        """没有已确认线索的记录不进反馈分母——反馈的入口只对 accepted 开放。"""
        post = make_post(self.reporter, status='matched')
        make_lead(post, self.helper, status='pending')

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        rows = {row['key']: row for row in result['feedback']['rows']}
        self.assertEqual(rows['still_matched']['accepted_count'], 0)
        self.assertIsNone(rows['still_matched']['feedback_rate'])

    # ---------------------------------------------------------------- 清单

    def test_list_is_sorted_by_overdue_descending(self):
        """清单按过期多久降序，最该核实的排最前。"""
        for days in (5, 30, 15):
            make_post(
                self.reporter, status='matched',
                expires_at=self.now - timedelta(days=days),
                title=f'过期 {days} 天的记录',
            )

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        labels = [row['overdue_label'] for row in result['list']['rows']]
        self.assertEqual(labels, ['30 天', '15 天', '5 天'])
        self.assertEqual(result['list']['row_total'], 3)

    def test_list_never_leaks_titles_or_usernames(self):
        """清单只带记录 ID 和过期多久，不带标题、不带发布者。"""
        post = make_post(
            self.reporter, status='matched',
            expires_at=self.now - timedelta(days=40),
            title='丢失身份证 330103199901011234',
        )

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        self.assertNotIn('身份证', repr(result['list']))
        self.assertNotIn(self.reporter.username, repr(result['list']))
        row = result['list']['rows'][0]
        self.assertEqual(row['post_id'], post.pk)
        self.assertEqual(row['overdue_label'], '40 天')

    def test_list_is_truncated_with_total_reported(self):
        """清单有上限，上限之下的总量一起报，否则模板里「完整的在导出里」没有依据。"""
        for index in range(LIST_LIMIT + 5):
            make_post(
                self.reporter, status='matched',
                expires_at=self.now - timedelta(days=1, hours=index),
            )

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        self.assertEqual(result['list']['row_total'], LIST_LIMIT + 5)
        self.assertEqual(len(result['list']['rows']), LIST_LIMIT)
        self.assertEqual(result['list']['row_limit'], LIST_LIMIT)

    # ---------------------------------------------------------------- 汇总与门槛

    def test_summary_mentions_still_matched_and_oldest_overdue(self):
        """汇总一句话要把「仍标记已匹配」和最久的一条都说到。"""
        make_post(
            self.reporter, status='matched',
            expires_at=self.now - timedelta(days=45),
        )

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        self.assertIn('仍标记已匹配', result['summary'])
        self.assertIn('45 天', result['summary'])

    def test_invalid_days_falls_back_to_default(self):
        """周期参数不在允许集合里时回落到 30 天，不抛异常。"""
        result = build_lost_found_matched_outcome(days=999, now=self.now)

        self.assertEqual(result['period_days'], 30)

    def test_min_matched_posts_threshold_is_exported(self):
        """门槛值要透出给模板，否则模板写死一个数字会和模块漂移。"""
        result = build_lost_found_matched_outcome(days=30, now=self.now)

        self.assertEqual(result['min_matched_posts'], MIN_MATCHED_POSTS)
        self.assertLess(MIN_MATCHED_POSTS, 100)

    def test_expired_matched_post_is_reported_as_expired_not_still_matched(self):
        """照实判断：一条真的变成 expired 的匹配记录归「已过期」，不归「仍标记」。"""
        counterpart = make_post(self.helper, post_type='found', status='matched')
        expired = make_post(
            self.reporter, status='expired',
            expires_at=self.now - timedelta(days=5),
            matched_post=counterpart,
        )
        counterpart.matched_post = expired
        counterpart.save(update_fields=['matched_post'])

        result = build_lost_found_matched_outcome(days=30, now=self.now)

        expired = next(r for r in result['outcome_rows'] if r['key'] == 'expired')
        self.assertEqual(expired['post_count'], 1)
        still = next(r for r in result['outcome_rows'] if r['key'] == 'still_matched')
        # counterpart 自己也还是 matched，所以「仍标记已匹配」是 1 条，不是 0 条。
        self.assertEqual(still['post_count'], 1)
        # 过期多久的分母是「仍标记已匹配」这一档，所以过期的那一条不计入。
        self.assertEqual(result['overdue_stats']['measured_count'], 0)
        self.assertEqual(result['overdue_stats']['no_deadline_count'], 1)