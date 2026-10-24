"""比对备份恢复出来的库与源库：逐表行数、索引数、约束数。

回答的问题是「dump 里的每一张表、索引、约束能不能原样恢复」，不是「业务数据
对不对」。恢复目标是一个全新库，因此两边都从 pg_tables / pg_indexes /
pg_constraint 现读，不依赖任何预先写死的表名清单。
"""

import os
from urllib.parse import urlsplit, urlunsplit

import psycopg

RESTORE_DB = 'campus_glimmer_restore'


def snapshot(dsn):
    """public schema 每张表的行数，外加索引与约束总数。"""
    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select tablename from pg_tables "
                "where schemaname = 'public' order by tablename")
            tables = [row[0] for row in cursor.fetchall()]
            counts = {}
            for table in tables:
                cursor.execute('select count(*) from "%s"' % table)
                counts[table] = cursor.fetchone()[0]
            cursor.execute(
                "select count(*) from pg_indexes "
                "where schemaname = 'public'")
            indexes = cursor.fetchone()[0]
            cursor.execute(
                'select count(*) from pg_constraint c '
                'join pg_namespace n on n.oid = c.connamespace '
                "where n.nspname = 'public'")
            constraints = cursor.fetchone()[0]
            return counts, indexes, constraints


def main():
    source_dsn = os.environ['DJANGO_DATABASE_URL']
    parts = urlsplit(source_dsn)
    target_dsn = urlunsplit(
        (parts.scheme, parts.netloc, '/' + RESTORE_DB, '', ''))

    source_counts, source_indexes, source_constraints = snapshot(source_dsn)
    target_counts, target_indexes, target_constraints = snapshot(target_dsn)

    migrations = source_counts.get('django_migrations', 0)
    if len(source_counts) < 5 or migrations < 50:
        raise SystemExit(
            '源库不像一个跑完迁移的库：%d 张表，django_migrations %d 条'
            % (len(source_counts), migrations))

    problems = []
    for table in sorted(set(source_counts) | set(target_counts)):
        if source_counts.get(table) != target_counts.get(table):
            problems.append('%s: 源库 %s 行，恢复库 %s 行' % (
                table, source_counts.get(table),
                target_counts.get(table)))
    if source_indexes != target_indexes:
        problems.append('索引数源库 %d，恢复库 %d'
                        % (source_indexes, target_indexes))
    if source_constraints != target_constraints:
        problems.append('约束数源库 %d，恢复库 %d'
                        % (source_constraints, target_constraints))

    print('比对 %d 张表、%d 个索引、%d 条约束，django_migrations %d 条'
          % (len(source_counts), source_indexes,
             source_constraints, migrations))
    if problems:
        raise SystemExit('隔离恢复结果与源库不一致：\n'
                         + '\n'.join(problems))
    print('隔离恢复结果与源库一致')


main()
