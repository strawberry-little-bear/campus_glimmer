from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.http import Http404
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.urls import reverse
from django.views import defaults
from django.views.csrf import csrf_failure


@override_settings(DEBUG=False, ALLOWED_HOSTS=['testserver'])
class BrandedErrorPageTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().get('/missing-page/')
        self.request.user = AnonymousUser()

    def assert_branded_page(self, response, *, status, code, heading):
        self.assertEqual(response.status_code, status)
        html = response.content.decode('utf-8')
        self.assertIn('拾光校园', html)
        # 生产环境启用 Manifest 静态文件存储，文件名会带内容哈希。
        self.assertRegex(
            html,
            r'css/error-pages(?:\.[0-9a-f]{12})?\.css',
        )
        self.assertIn(f'error-code">{code}<', html)
        self.assertIn(heading, html)
        self.assertIn('href="/listings/"', html)
        self.assertNotIn('Traceback', html)
        self.assertNotIn('Exception Value', html)

    def test_unknown_route_uses_branded_404(self):
        response = Client().get('/definitely-not-a-real-page/')

        self.assert_branded_page(response, status=404, code='404', heading='这束光可能去了别处')
        self.assertTemplateUsed(response, '404.html')

    def test_bad_request_uses_branded_400(self):
        response = defaults.bad_request(self.request, SuspiciousOperation('invalid request'))

        self.assert_branded_page(response, status=400, code='400', heading='这个请求没能被识别')

    def test_permission_denied_uses_branded_403(self):
        response = defaults.permission_denied(self.request, PermissionDenied('blocked'))

        self.assert_branded_page(response, status=403, code='403', heading='当前账号不能打开这里')
        self.assertNotIn('blocked', response.content.decode('utf-8'))

    def test_server_error_uses_branded_500_without_technical_details(self):
        response = defaults.server_error(self.request)

        self.assert_branded_page(response, status=500, code='500', heading='这次请求没有完成')

    def test_csrf_failure_uses_recovery_oriented_403(self):
        response = csrf_failure(self.request, reason='CSRF token missing')

        self.assert_branded_page(response, status=403, code='403', heading='这次提交需要重新确认')
        self.assertIn('返回上一页刷新后重新提交', response.content.decode('utf-8'))
        self.assertNotIn('CSRF token missing', response.content.decode('utf-8'))

    def test_real_csrf_rejection_uses_custom_page(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse('login'), {'username': 'demo', 'password': 'not-a-real-password'})

        self.assert_branded_page(response, status=403, code='403', heading='这次提交需要重新确认')
        self.assertTemplateUsed(response, '403_csrf.html')

    def test_not_found_handler_uses_branded_404(self):
        response = defaults.page_not_found(self.request, Http404('missing'))

        self.assert_branded_page(response, status=404, code='404', heading='这束光可能去了别处')
