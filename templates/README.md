# templates · 服务端渲染页面

[返回项目首页](../README.md) · [完整项目手册](../docs/README.md) · [模块地图](../docs/module-map.md)

> 页面以 Django Templates 服务端渲染为主，JavaScript 只做渐进增强。模板负责结构与展示，权限、状态流转和数据口径必须在 Python 服务端实现。

## 目录结构

| 目录 | 内容 |
| --- | --- |
| `base.html` | 全站导航、消息提示、未读角标、页脚和公共脚本 |
| `accounts/` | 注册、登录、资料、公开主页和校园认证 |
| `chat_messages/` | 收件箱、会话、审核队列和申诉页面 |
| `listings/` | 商品、搜索、订单、通知、运营看板和治理页面 |
| `listings/_item_card.html` | 商品卡片复用片段 |

## 模板约定

- 页面模板优先 `{% extends 'base.html' %}`；
- 使用 `{% url 'name' %}` 生成链接，不硬编码业务路径；
- 使用 `{% static '...' %}` 引用静态资源；
- 可复用片段使用下划线前缀，例如 `_item_card.html`；
- 公共结构放入 `base.html`，页面专属脚本放入 `extra_js`；
- 模板中不复制权限判断作为唯一防线，服务端仍需再次校验；
- 修改未读角标、导航或全局脚本时，要同时检查 `base.html` 与 `static/js/main.js`。

## 常用 block

| Block | 用途 |
| --- | --- |
| `title` | 页面标题 |
| `extra_css` | 页面专属样式 |
| `content` | 主内容区 |
| `extra_js` | 页面专属脚本 |

## 验证

```powershell
python manage.py check
python manage.py test
python manage.py runserver
```

视觉改动建议同时查看桌面与窄屏页面，并确认[系统截图](../docs/screenshots/)仍能代表当前界面。

## 相关入口

- [静态资源说明](../static/README.md)
- [截图维护说明](../docs/screenshots/README.md)
- [功能手册](../docs/feature-handbook.md)
