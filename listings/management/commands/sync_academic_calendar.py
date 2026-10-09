from django.core.management.base import BaseCommand

from listings.academic_calendar import (
    ensure_default_phases,
    refresh_academic_term_snapshots,
)
from listings.models import AcademicTerm


class Command(BaseCommand):
    help = ('维护学期日历：为启用中的学期补齐默认阶段，并刷新各阶段统计快照。')

    def add_arguments(self, parser):
        parser.add_argument(
            '--term-slug', default=None,
            help='只处理指定学期标识，默认处理全部启用中的学期。',
        )
        parser.add_argument(
            '--ensure-phases', action='store_true',
            help='按默认阶段计划补齐缺失的学期阶段。',
        )
        parser.add_argument(
            '--refresh-snapshots', action='store_true',
            help='按最新业务数据刷新学期阶段统计快照。',
        )

    def handle(self, *args, **options):
        ensure_phases = options.get('ensure_phases')
        refresh_snapshots = options.get('refresh_snapshots')
        if not ensure_phases and not refresh_snapshots:
            ensure_phases = True
            refresh_snapshots = True

        slug = options.get('term_slug')
        terms = AcademicTerm.objects.filter(is_active=True).order_by('-starts_on')
        if slug:
            terms = terms.filter(slug=slug)
        terms = list(terms)

        if not terms:
            self.stdout.write(self.style.WARNING('没有找到启用中的学期，未做任何处理。'))
            return

        created_phases = 0
        if ensure_phases:
            for term in terms:
                created_phases += ensure_default_phases(term)

        snapshots = 0
        if refresh_snapshots:
            snapshots = refresh_academic_term_snapshots()

        parts = [f'处理学期 {len(terms)} 个']
        if ensure_phases:
            parts.append(f'新增阶段 {created_phases} 条')
        if refresh_snapshots:
            parts.append(f'刷新阶段快照 {snapshots} 条')
        self.stdout.write(self.style.SUCCESS('，'.join(parts) + '。'))