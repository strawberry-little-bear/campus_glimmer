from datetime import timedelta
from urllib.parse import urlsplit

from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import CampusDomain, CampusVerification


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
