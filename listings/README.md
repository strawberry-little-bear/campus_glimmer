# listings · 校园交易与运营核心

[返回项目首页](../README.md) · [完整项目手册](../docs/README.md) · [模块地图](../docs/module-map.md)

> `listings/` 是平台最大的业务应用，覆盖商品、求购、失物招领、订单、通知、治理和运营分析。它不是一个单纯的“商品表”，而是一条可追踪的校园交易链路。

## 先看这里

| 文件 | 作用 |
| --- | --- |
| `models.py` | 商品、分类、校园地点、订单、通知、举报、专题等核心模型 |
| `views.py` | 用户页面、交易操作、运营看板和治理入口 |
| `urls.py` | `/listings/` 下全部页面与接口路由 |
| `forms.py` | 商品、搜索、订单、通知偏好等表单 |
| `admin.py` | Django 后台维护入口 |
| `management/commands/` | 定时任务与数据回填命令 |
| `test_*.py` | 按业务能力拆分的回归测试 |

## 业务分组

| 方向 | 代表模块 |
| --- | --- |
| 商品与供给 | `item_lifecycle`、`availability`、`image_processing`、`bulk_listing_*`、`relist_freshness` |
| 搜索与发现 | `search_*`、`saved_searches`、`recommendations`、`opportunity_feed`、`price_insights` |
| 求购与失物 | `demand_*`、`lost_found_*`、`demand_radar_*` |
| 订单与交付 | `order_workflow`、`meeting_scheduling`、`delivery_handshake`、`return_handshake`、`transaction_safety` |
| 通知与摘要 | `notifications`、`notification_*`、`*_digest`、`order_deadline_reminders` |
| 治理与安全 | `report_priority`、`governance_sla`、`moderation_*`、`dispute_*` |
| 运营与数据质量 | `analytics`、`*_quality`、`*_trend`、`task_run_*`、`dashboard_build_performance` |

细节清单见[模块地图](../docs/module-map.md)，完整规则见[功能手册](../docs/feature-handbook.md)。

## 数据与实现纪律

- 订单状态变更、交付确认、借用归还和争议处理必须在服务端校验身份与当前状态；
- 运营看板和统计模块以“只报告”为默认原则，不自动改写被观测的业务数据；
- 每个指标要明确分母、时间窗口、样本门槛和缺失值语义；
- 交易写入优先使用事务与必要的行级锁，避免并发下重复确认；
- 新能力优先拆成独立领域模块和对应 `test_*.py`，不要把更多职责继续堆进单一视图；
- 涉及图片时统一复用 `image_processing.py`，不要在各页面重复实现压缩和方向修正。

## 定时任务

| 命令 | 用途 |
| --- | --- |
| `process_order_timeouts` | 预约超时、借用到期与逾期提醒 |
| `expire_items` | 下架到期商品并通知发布者 |
| `send_operations_digest` | 发送管理员运营告警摘要 |
| `send_opportunity_digest` | 发送互助机会摘要 |
| `send_saved_search_digest` | 发送关注搜索汇总 |
| `send_lifecycle_reminders` | 发送商品生命周期提醒 |
| `sync_academic_calendar` | 同步学期阶段和统计快照 |
| `backfill_item_thumbnails` | 回填历史图片缩略图和响应式变体 |

参数、幂等边界和运行台账见[后台与定时任务](../docs/scheduled-tasks.md)。

## 验证

```powershell
python manage.py test listings
python manage.py test listings.test_order_stage_flow
python manage.py makemigrations --check --dry-run
python manage.py check
```

完整测试量较大时，先运行受影响能力对应的 `test_*.py`，提交前再运行全量测试。

## 延伸阅读

- [功能手册](../docs/feature-handbook.md)
- [核心数据模型](../docs/data-model.md)
- [后台与定时任务](../docs/scheduled-tasks.md)
- [当前取舍与边界](../docs/product-boundaries.md)
- [后续路线图](../docs/roadmap.md)
