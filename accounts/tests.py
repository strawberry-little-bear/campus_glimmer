from datetime import timedelta
from urllib.parse import urlsplit

from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import CampusDomain, CampusVerification
from listings.models import Category, CampusLocation, DemandPost, DemandResponse, Item, MutualAidFeedback, Order, Rating
from listings.reputation import build_user_reputation


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class CampusVerificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='alice',
            email='alice@example.com',
            password='strong-password-123',
        )
        self.other_user = User.objects.create_user(
            username='bob',
            email='bob@example.com',
            password='strong-password-123',
        )
        self.domain = CampusDomain.objects.create(
            domain='university.edu.cn',
            name='示例大学',
        )
        self.url = reverse('campus_verification')
        self.client.force_login(self.user)

    def request_verification(self, email='alice@university.edu.cn'):
        response = self.client.post(self.url, {'campus_email': email})
        self.assertRedirects(response, self.url)
        self.assertEqual(len(mail.outbox), 1)
        verification = CampusVerification.objects.get(user=self.user)
        verification_url = mail.outbox[-1].body.split('\n\n')[1].split('\n')[0]
        return verification, verification_url

    def test_rejects_email_when_domain_is_not_enabled(self):
        CampusDomain.objects.update(is_active=False)

        response = self.client.post(self.url, {'campus_email': 'alice@university.edu.cn'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '暂不支持该邮箱域名')
        self.assertFalse(CampusVerification.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_sends_email_and_stores_only_token_hash(self):
        verification, verification_url = self.request_verification()
        raw_token = urlsplit(verification_url).path.rstrip('/').split('/')[-1]

        self.assertEqual(verification.status, 'pending')
        self.assertEqual(verification.campus_email, 'alice@university.edu.cn')
        self.assertEqual(verification.domain_name, '示例大学')
        self.assertTrue(verification.token_hash)
        self.assertNotEqual(verification.token_hash, raw_token)
        self.assertTrue(check_password(raw_token, verification.token_hash))
        self.assertEqual(mail.outbox[0].to, ['alice@university.edu.cn'])

    def test_verification_link_marks_identity_verified(self):
        verification, verification_url = self.request_verification()

        response = self.client.get(urlsplit(verification_url).path)
        verification.refresh_from_db()

        self.assertRedirects(response, reverse('profile'))
        self.assertEqual(verification.status, 'verified')
        self.assertTrue(verification.verified_at)
        self.assertEqual(verification.token_hash, '')

    def test_wrong_token_does_not_verify_identity(self):
        verification, verification_url = self.request_verification()
        path_parts = urlsplit(verification_url).path.rstrip('/').split('/')
        path_parts[-1] = 'invalid-token'

        response = self.client.get('/'.join(path_parts) + '/')
        verification.refresh_from_db()

        self.assertRedirects(response, self.url)
        self.assertEqual(verification.status, 'pending')
        self.assertIsNone(verification.verified_at)

    def test_expired_token_does_not_verify_identity(self):
        verification, verification_url = self.request_verification()
        CampusVerification.objects.filter(pk=verification.pk).update(
            token_issued_at=timezone.now() - timedelta(hours=25),
        )

        response = self.client.get(urlsplit(verification_url).path)
        verification.refresh_from_db()

        self.assertRedirects(response, self.url)
        self.assertEqual(verification.status, 'pending')
        self.assertIsNone(verification.verified_at)

    def test_same_campus_email_cannot_be_bound_to_another_user(self):
        self.request_verification()
        self.client.force_login(self.other_user)

        response = self.client.post(self.url, {'campus_email': 'ALICE@UNIVERSITY.EDU.CN'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '已经绑定了其他账号')
        self.assertFalse(CampusVerification.objects.filter(user=self.other_user).exists())
        self.assertEqual(len(mail.outbox), 1)


class PublicProfileTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='seller', email='seller@example.com', password='strong-password-123',
        )
        self.other_user = User.objects.create_user(
            username='buyer', email='buyer@example.com', password='strong-password-123',
        )
        self.category = Category.objects.create(name='公共资料测试分类')
        self.location = CampusLocation.objects.create(name='公共资料测试地点')

    def test_public_profile_shows_explainable_trade_record_without_private_contact_data(self):
        item = Item.objects.create(
            seller=self.user, title='公开档案测试商品', description='测试描述', price='20',
            category=self.category, location=self.location, condition='9成新',
        )
        order = Order.objects.create(
            item=item, buyer=self.other_user, seller=self.user,
            agreed_price='20', status='completed',
        )
        Rating.objects.create(
            order=order, rater=self.other_user, ratee=self.user,
            score=5, comment='沟通顺利',
        )

        response = self.client.get(reverse('public_profile', args=[self.user.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'seller的交易档案')
        self.assertContains(response, '公开档案测试商品')
        self.assertContains(response, '完成交易')
        self.assertContains(response, '沟通顺利')
        self.assertNotContains(response, 'seller@example.com')

    def test_public_profile_includes_privacy_safe_mutual_aid_summary(self):
        demand = DemandPost.objects.create(
            requester=self.other_user,
            title='公开互助摘要求购',
            description='测试公开互助结果摘要。',
            category=self.category,
            location=self.location,
        )
        item = Item.objects.create(
            seller=self.user,
            title='公开互助摘要商品',
            description='测试描述',
            price='20',
            category=self.category,
            location=self.location,
            condition='9成新',
        )
        response = DemandResponse.objects.create(
            demand=demand, item=item, responder=self.user, status='accepted',
        )
        MutualAidFeedback.objects.create(
            demand_response=response,
            submitted_by=self.other_user,
            outcome='completed',
            tags=['on_time', 'helpful'],
            note='不应在公开档案展示。',
        )

        profile = self.client.get(reverse('public_profile', args=[self.user.id]))

        self.assertEqual(profile.status_code, 200)
        self.assertContains(profile, '校园互助反馈')
        self.assertContains(profile, '完成互助')
        self.assertContains(profile, '100.0%')
        self.assertNotContains(profile, '不应在公开档案展示')

    def test_user_reputation_combines_buyer_and_seller_history(self):
        sold_item = Item.objects.create(
            seller=self.user, title='卖出商品', description='测试描述', price='20',
            category=self.category, location=self.location, condition='9成新',
        )
        bought_item = Item.objects.create(
            seller=self.other_user, title='买入商品', description='测试描述', price='30',
            category=self.category, location=self.location, condition='9成新',
        )
        sold_order = Order.objects.create(
            item=sold_item, buyer=self.other_user, seller=self.user,
            agreed_price='20', status='completed',
        )
        bought_order = Order.objects.create(
            item=bought_item, buyer=self.user, seller=self.other_user,
            agreed_price='30', status='returned',
        )
        Rating.objects.create(order=sold_order, rater=self.other_user, ratee=self.user, score=5)
        Rating.objects.create(order=bought_order, rater=self.other_user, ratee=self.user, score=4)

        reputation = build_user_reputation(self.user)

        self.assertEqual(reputation['completed_orders'], 2)
        self.assertEqual(reputation['seller_completed_orders'], 1)
        self.assertEqual(reputation['buyer_completed_orders'], 1)
        self.assertEqual(reputation['rating_count'], 2)
        self.assertEqual(reputation['rating_average'], 4.5)
        self.assertIn('有完成交易记录', reputation['badges'])
