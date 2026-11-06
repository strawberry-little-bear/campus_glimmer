# campus_glimmer/test_observability.py
"""集中日志叠加层与生产日志格式的静态契约。"""

import re
from pathlib import Path

from django.test import SimpleTestCase


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBSERVABILITY_COMPOSE = PROJECT_ROOT / 'docker-compose.observability.yml'
PRODUCTION_COMPOSE = PROJECT_ROOT / 'docker-compose.prod.yml'
ALLOY_CONFIG = (
    PROJECT_ROOT / 'docker' / 'observability' / 'alloy' / 'config.alloy'
)
LOKI_CONFIG = PROJECT_ROOT / 'docker' / 'observability' / 'loki' / 'config.yml'
DATASOURCE = (
    PROJECT_ROOT / 'docker' / 'observability' / 'grafana' /
    'provisioning' / 'datasources' / 'loki.yml'
)


class ObservabilityStackContractTests(SimpleTestCase):
    def setUp(self):
        self.compose = OBSERVABILITY_COMPOSE.read_text(encoding='utf-8')
        self.production = PRODUCTION_COMPOSE.read_text(encoding='utf-8')

    def test_images_are_pinned_and_the_three_services_are_present(self):
        for expected in (
            'image: grafana/loki:3.7.9',
            'image: grafana/alloy:v1.6.1',
            'image: grafana/grafana:13.3.0',
            '  loki:',
            '  alloy:',
            '  grafana:',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.compose)
        self.assertNotIn('image: grafana/loki:latest', self.compose)

    def test_all_published_ports_are_bound_to_loopback(self):
        port_lines = [
            line.strip() for line in self.compose.splitlines()
            if re.match(r'^\s+-\s+"[^"]+:[0-9]+"\s*$', line)
        ]
        self.assertEqual(len(port_lines), 3, port_lines)
        for line in port_lines:
            with self.subTest(port=line):
                self.assertIn('127.0.0.1:', line)

    def test_grafana_password_must_be_supplied(self):
        self.assertIn(
            '${GRAFANA_ADMIN_PASSWORD:?', self.compose,
            'Grafana 不能使用空密码或仓库内固定密码启动',
        )
        self.assertIn(
            'GF_AUTH_ANONYMOUS_ENABLED: "false"', self.compose,
            '日志系统不应允许匿名访问',
        )

    def test_alloy_only_collects_this_compose_project(self):
        alloy = ALLOY_CONFIG.read_text(encoding='utf-8')
        self.assertIn('discovery.docker "containers"', alloy)
        self.assertIn(
            '__meta_docker_container_label_com_docker_compose_project', alloy)
        self.assertIn('regex         = "campus-glimmer-prod"', alloy)
        self.assertIn('action        = "keep"', alloy)
        self.assertIn('http://loki:3100/loki/api/v1/push', alloy)

    def test_docker_socket_is_read_only_but_documented_as_privileged(self):
        self.assertIn(
            '/var/run/docker.sock:/var/run/docker.sock:ro', self.compose)
        alloy = ALLOY_CONFIG.read_text(encoding='utf-8')
        self.assertIn('Docker API', alloy)
        self.assertIn('宿主机权限', alloy)

    def test_loki_retains_seven_days_and_compacts_deleted_data(self):
        loki = LOKI_CONFIG.read_text(encoding='utf-8')
        self.assertIn('retention_period: 168h', loki)
        self.assertIn('retention_enabled: true', loki)
        self.assertIn('delete_request_store: filesystem', loki)

    def test_grafana_datasource_points_at_the_private_loki_service(self):
        datasource = DATASOURCE.read_text(encoding='utf-8')
        self.assertIn('type: loki', datasource)
        self.assertIn('url: http://loki:3100', datasource)
        self.assertIn('isDefault: true', datasource)

    def test_production_app_emits_json_without_changing_local_default(self):
        self.assertIn('DJANGO_LOG_FORMAT: "json"', self.production)
        env_template = (PROJECT_ROOT / '.env.production.example').read_text(
            encoding='utf-8')
        self.assertIn('DJANGO_LOG_FORMAT=json', env_template)
