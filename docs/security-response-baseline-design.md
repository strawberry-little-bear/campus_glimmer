# 安全响应头基线设计

状态：设计稿（本轮先明确口径，不改代码）

## 1. 背景

项目已经把「存活」和「数据库就绪」拆成两个探针，也提供了 HTTPS 重定向与 Cookie Secure 的环境变量。但浏览器安全边界仍处于“部署时再想起来”的状态：默认配置里没有始终生效的 MIME 嗅探防护、Referrer 策略、跨源窗口隔离和更严格的 framing 边界。

这些响应头不依赖业务表结构，也不需要新增中间件依赖；Django 自带 `SecurityMiddleware` 与 `XFrameOptionsMiddleware` 已经能提供大部分能力。缺的是明确基线、环境变量边界和测试，避免每次部署各配各的。

## 2. 目标

为所有 HTML、JSON、静态资源和媒体响应提供一组默认即安全的响应头：

- `X-Content-Type-Options: nosniff`：禁止浏览器猜测资源类型；
- `Referrer-Policy: same-origin`：站内导航保留 Referrer，出站链接不携带本站 URL 与查询参数；
- `Cross-Origin-Opener-Policy: same-origin`：限制跨源窗口访问顶层浏览上下文；
- `X-Frame-Options: DENY`：公开页面与运营页面都不允许被 iframe 嵌入；
- 会话 Cookie 与 CSRF Cookie 默认 `HttpOnly`，脚本不直接读取令牌；
- HTTPS 部署可通过 `DJANGO_SECURE_HSTS_SECONDS` 显式启用 HSTS，并在启用后带 `includeSubDomains` 与 `preload`。

本地 HTTP 开发默认不发送 HSTS，避免第一次访问 127.0.0.1 就写入长期浏览器状态。

## 3. 配置边界

- 安全基线不跟随 `DEBUG` 开关：本地、测试、预发和生产都应得到同一组基础头；
- `DJANGO_SECURE_HSTS_SECONDS` 只接受非负整数，非法值回落为 0 并保持服务可用；
- HSTS 仅在运维确认证书、子域名和回滚窗口后配置，建议从较短时长开始，再提升到一年；
- 现有 `DJANGO_SECURE_SSL_REDIRECT`、`DJANGO_SESSION_COOKIE_SECURE`、`DJANGO_CSRF_COOKIE_SECURE` 继续独立配置，不互相推导。

## 4. 暂不做 CSP 的原因

本轮不添加 Content-Security-Policy。当前模板包含 CDN 字体、Bootstrap 资源以及若干模板内脚本与样式，未经清单化就配置严格 CSP，容易在生产造成静默破版；而 Report-Only 若没有上报后端，也只是一条不会形成闭环的响应头。

后续可以在本轮基线上单独迭代：先盘点脚本与样式来源，再引入 nonce 或 hash，最后按“Report-Only 观察 → 强制”的顺序启用。

## 5. 测试与验收

- 访问 `/healthz/` 时能看到四项基础安全头；
- 默认配置不发送 `Strict-Transport-Security`；
- 配置 HSTS 秒数后，响应包含 max-age、`includeSubDomains` 与 `preload`；
- 健康检查原有的 `Cache-Control: no-store` 与错误脱敏行为不受影响；
- 不新增表、字段、迁移或外部依赖。
