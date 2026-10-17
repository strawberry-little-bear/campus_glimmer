# .github · 持续集成

[返回项目首页](../README.md) · [完整项目手册](../docs/README.md) · [模块地图](../docs/module-map.md)

> 当前仓库使用 GitHub Actions 对主分支和 Pull Request 做统一质量检查。

## 工作流

`workflows/django.yml` 在以下场景触发：

- 推送到 `main`；
- 向 `main` 提交 Pull Request。

`quality` 任务依次执行：

1. 拉取代码并配置 Python 3.12；
2. 使用 pip 缓存安装 `requirements.txt`；
3. 对四个 Python 包执行语法检查；
4. 执行 `manage.py check`；
5. 检查是否存在未生成的迁移；
6. 运行完整 Django 测试集。

## 本地等价检查

提交前建议运行：

```powershell
python -m compileall -q accounts campus_glimmer chat_messages listings
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

## 修改约定

- 工作流只做验证，不自动修改代码、迁移或数据；
- 不把令牌、生产密钥或真实用户数据写进 workflow；
- 新增运行依赖时同步更新 `requirements.txt` 和 CI；
- 调整 Python 版本、Action 版本或测试命令时，先用同一组本地命令验证；
- CI 失败时优先修复第一个真实错误，不要通过跳过测试或放宽检查来“变绿”。
