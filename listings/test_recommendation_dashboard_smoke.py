# -*- coding: utf-8 -*-
"""冒烟：确认看板面板与 CSV 在新字段下都能渲染/导出，不报 KeyError。"""
import io
import csv

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Category, CampusLocation, Item, RecommendationFeedback, RecommendationImpression


class RecommendationDashboardSmokeTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='smoke-admin', password='safe-password-123', email='a@b.c',
        )
        self.seller = User.objects.create_user(username='smoke-seller')
        self.category = Category.objects.create(name='冒烟分类')
        self.location = CampusLocation.objects.create(name='冒烟地点', building='楼', is_public=True)

    def test_dashboard_and_export_render_with_impressions(self):
        for index in range(30):
            item = Item.objects.create(
                title=f'冒烟商品 {index}', description='d', price='20.00',
                category=self.category, location=self.location, condition='良好',
                seller=self.seller, status='available',
            )
            RecommendationImpression.objects.create(
                user=self.admin, item=item, reason_key='fresh', score=2.0, position=1,
            )
            if index % 3 == 0:
                RecommendationFeedback.objects.create(user=self.admin, item=item, action='interested')

        self.client.force_login(self.admin)
        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 200)
        quality = response.context['recommendation_quality']
        self.assertEqual(quality['feedback_loop']['viewed_recommendations'], 30)
        self.assertEqual(len(quality['reason_impression_rows']), 6)
        self.assertTrue(any(row['shown_count'] for row in quality['score_bands']))

        export = self.client.get(reverse('operations_dashboard_export'))
        self.assertEqual(export.status_code, 200)
        text = export.content.decode('utf-8-sig')
        self.assertIn('按推荐当时的理由', text)
        self.assertIn('按推荐当时的分数档', text)
        self.assertIn('被看见的卡', text)
        self.assertIn('曝光样本门槛（次被看见）', text)

    def test_dashboard_and_export_render_on_an_empty_campus(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse('operations_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['recommendation_quality']['has_data'])

        export = self.client.get(reverse('operations_dashboard_export'))
        self.assertEqual(export.status_code, 200)
        text = export.content.decode('utf-8-sig')
        self.assertIn('首页推荐卡挑得准不准', text)