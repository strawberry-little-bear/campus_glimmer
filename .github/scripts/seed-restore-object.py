"""给备份链路准备一个真实的对象，供对象镜像恢复后按原 key 逐字节比对。

migrate 之后库里已经有 django_migrations、django_content_type 和
auth_permission，足够回答「dump 能不能原样恢复」；但对象存储那一侧还是空的，
所以这里真实上传一个对象，让恢复演练也覆盖媒体文件。
"""

import os
import sys
from pathlib import Path

# 直接 `python .github/scripts/xxx.py` 运行时，Python 只把脚本所在目录放进
# sys.path[0]，仓库根不在路径里，`import campus_glimmer` 会失败。这里显式把
# 仓库根补进 sys.path，让脚本从仓库任何位置被调用都能导入设置模块。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'campus_glimmer.settings')

import django

django.setup()

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

OBJECT_KEY = 'ci-restore-proof.txt'
OBJECT_BODY = b'campus glimmer ci restore proof\n'

if default_storage.exists(OBJECT_KEY):
    default_storage.delete(OBJECT_KEY)

name = default_storage.save(OBJECT_KEY, ContentFile(OBJECT_BODY))
if name != OBJECT_KEY:
    raise SystemExit('上传后的 key 与预期不一致：%r' % name)
print('uploaded object key:', name)