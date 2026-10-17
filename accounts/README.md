# accounts · 账户与校园身份

[返回项目首页](../README.md) · [完整项目手册](../docs/README.md) · [模块地图](../docs/module-map.md)

> 负责“谁在使用平台”以及“校园身份是否可信”。商品、订单和社区治理由其他应用承载，这里只维护账户、公开资料和校园认证。

## 目录职责

- 注册、登录、退出和个人资料编辑；
- 为用户自动创建扩展资料 `Profile`；
- 维护允许认证的校园邮箱域名 `CampusDomain`；
- 通过带时效的邮件链接完成 `CampusVerification`；
- 聚合公开主页所需的信誉、贡献、在售商品与评价摘要。

## 关键入口

| 文件 | 作用 |
| --- | --- |
| `models.py` | `Profile`、`CampusDomain`、`CampusVerification` 及用户资料信号 |
| `forms.py` | 注册、资料更新和校园认证表单 |
| `views.py` | 注册、公开主页、资料编辑、认证邮件发送与验证 |
| `urls.py` | `/accounts/` 下的账户与认证路由 |
| `admin.py` | 校园域名和认证记录的运营入口 |
| `tests.py` | 账户、资料与校园认证测试 |

## 身份链路

1. 用户注册后，信号自动创建 `Profile`；
2. 输入校园邮箱时，只能选择已启用的 `CampusDomain`；
3. 系统生成一次性验证令牌，只保存哈希，并记录 24 小时有效期；
4. 邮件链接验证成功后写入 `verified_at`，清空令牌；
5. 公开主页只展示已验证标识，不暴露令牌、邮箱域名配置或其他账户私有数据。

## 修改时注意

- 认证状态必须由服务端写入，不能通过资料表单直接修改；
- 重新发送验证邮件时，应保证旧令牌失效；
- 公开主页的展示字段要经过隐私检查，不要顺手带出邮箱、订单或私信；
- 账户相关操作要同时补测试，尤其是过期令牌、错误用户和重复验证场景。

## 验证

```powershell
python manage.py test accounts
python manage.py check
```

## 延伸阅读

- [功能手册：校园身份与可信交易](../docs/feature-handbook.md#8-校园身份与可信交易)
- [校园认证摩擦设计](../docs/campus-verification-friction-design.md)
- [核心数据模型](../docs/data-model.md)
- [当前取舍与边界](../docs/product-boundaries.md)
