# chat_messages · 留言、私信与内容治理

[返回项目首页](../README.md) · [完整项目手册](../docs/README.md) · [模块地图](../docs/module-map.md)

> 承载商品留言、用户私信、未读状态、内容审核队列、误判放行与内容申诉。站内通知中心位于 `listings/`，两者职责不同。

## 目录职责

- 商品详情页留言；
- 用户之间围绕商品发起私信；
- 收件箱、会话详情、单条/批量/会话已读；
- 内容审核事件的风险分层、复核与释放；
- 用户对审核结果提交申诉，运营端完成处理闭环；
- 私信响应时效、响应场景和交易关联的只读报告。

## 关键入口

| 文件 | 作用 |
| --- | --- |
| `models.py` | `Comment`、`PrivateMessage`、`ModerationEvent`、`ModerationAppeal` |
| `views.py` | 收件箱、会话、发送私信、已读操作、审核和申诉流程 |
| `forms.py` | 私信与审核相关表单 |
| `urls.py` | `/messages/` 下全部路由 |
| `moderation.py` | 内容审核规则与风险信号 |
| `chat_response.py` | 私信响应时效统计 |
| `chat_response_trend.py` | 响应时效跨周期对比 |
| `chat_trade_correlation.py` | 沟通行为与交易结果的关联分析 |
| `test_*.py` | 私信、审核、申诉和统计的分项测试 |

## 主要路由

| 路径 | 用途 |
| --- | --- |
| `/messages/inbox/` | 收件箱与未读消息 |
| `/messages/conversation/<user_id>/` | 与指定用户的会话 |
| `/messages/send/<receiver_id>/` | 发起或继续私信 |
| `/messages/moderation/` | 内容审核队列 |
| `/messages/moderation/appeals/` | 内容申诉与处理 |

## 边界与约定

- 只有会话参与者可以查看对应私信，管理员权限不能替代参与关系；
- 已读操作只影响当前用户自己的接收状态；
- 审核事件保留来源、风险层级和处理留痕，释放内容不能伪造为“从未发生”；
- 未读角标通过页面脚本轮询刷新，是渐进增强；即使脚本失败，服务端渲染的初始状态仍然可用；
- 新增统计模块应保持只读，不把报告结果写回私信或审核表。

## 验证

```powershell
python manage.py test chat_messages
python manage.py test chat_messages.test_moderation_appeal
python manage.py check
```

## 延伸阅读

- [功能手册：私信、留言与通知中心](../docs/feature-handbook.md#5-私信留言与通知中心)
- [私信响应场景设计](../docs/chat-response-context-design.md)
- [审核风险分层设计](../docs/moderation-risk-tier-design.md)
- [内容申诉设计](../docs/moderation-appeal-design.md)
