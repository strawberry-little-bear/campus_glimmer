# static · 样式、脚本与基础图片

[返回项目首页](../README.md) · [完整项目手册](../docs/README.md) · [模块地图](../docs/module-map.md)

> 项目不依赖 Node.js 构建链，CSS、JavaScript 和图片直接由 Django 静态文件系统提供。修改后刷新页面即可，生产环境由 `collectstatic` 收集。

## 目录结构

| 路径 | 作用 |
| --- | --- |
| `css/style.css` | 全站主题、布局、组件和响应式样式 |
| `js/main.js` | 搜索建议、未读角标轮询和页面增强 |
| `img/default-avatar.png` | 默认头像 |
| `img/no-image.png` | 商品缺图占位 |

## 样式约定

- 颜色、阴影等主题值优先复用 `:root` 中的 CSS 变量；
- 新组件先确认是否能在现有 Bootstrap 组件和项目样式上扩展；
- 保持桌面与移动端断点行为一致，避免只在单一宽度下验证；
- 运营页面和用户页面的视觉层级应可区分，但不另起一套主题；
- 大段新样式应带用途说明，避免继续形成无法判断归属的规则堆积。

## 脚本约定

`main.js` 当前承担两类增强：

1. 搜索框的防抖建议、键盘导航和请求取消；
2. 每 30 秒刷新消息与通知未读角标，并在数量增加时显示提示。

脚本失败时应保持服务端渲染的初始状态可用。新增逻辑优先限定在对应的 `data-*` 容器内，不要在全局选择器上做破坏性操作。

## 验证

```powershell
python manage.py collectstatic --noinput
python manage.py check
python manage.py runserver
```

手动检查搜索建议、未读刷新、移动端导航和商品卡片布局。

## 相关入口

- [模板说明](../templates/README.md)
- [完整项目手册](../docs/README.md)
- [截图维护说明](../docs/screenshots/README.md)
