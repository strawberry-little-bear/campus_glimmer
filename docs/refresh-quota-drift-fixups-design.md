# 刷新额度模块的两个测试失败，修掉

## 起点

`refresh-quota-drift-design.md` 做完之后，`test_refresh_quota_drift.py` 里有
25 个测试，其中两个一直是红的：

```
FAIL: test_the_list_is_ordered_by_how_long_the_listing_has_sunk
      [59, 59, 59] != [60, 60, 60]

FAIL: test_refresh_count_is_never_reset_anywhere
      ['.\listings\relist_freshness.py', '.\listings\test_relist_freshness.py'] != []
```

两个都不是本轮引入的，从 `d718dd35c` 挂到现在。本轮把它们修掉，两条测试都
保持它们原本的断言值——因为两个失败里，被测对象各有一个真问题，而测试本身
只有一个问题。

## 失败一：同一行里两个口径不一致

`_build_drift_list` 给运营的清单里，一行同时有 `since_refresh_days` 和
`age_days` 两列。同一个对象、同一行、同一个看板，两个数却可能差一天：

- `since_refresh_days` 走 `_days_between(now, item.last_refreshed_at)`，
  参考时刻是**传进模块的 `now`**；
- `age_days` 走 `item.freshness_age_days`，而那个属性内部读的是
  **`timezone.now()`**。

测试用 `build_refresh_quota_drift(days=30, now=self.now)` 把 `now` 钉在
`local_noon()`，于是两个时刻差了几小时。`_days_between` 算出的是带小数的
天数，`since_refresh_days` 那边 `int(max(..., 0))` 截断，`freshness_age_days`
那边 `delta.days` 也截断——但截的是不同的起点，60.0 天就一边得 60、一边得
59。

同一个对象在同一行里两个口径不一致，比两个数都错更糟：运营没法判断该信
哪一个，而这两个数在看板上是挨着印的。

### 修法

`age_days` 改用模块自己的 `now`，新增一个模块内的 `_sunk_days`：

```python
def _sunk_days(item, *, now):
    return int(max(_days_between(now, item.last_active_at), 0))
```

排序也换成同一个函数。排序和展示用两个不同的值，是同一类错误的另一种
形状——本例三件都相等所以看不出来，但那是偶然。

不选另外两条路：

- **不给 `freshness_age_days` 加 `now` 参数**。它有 11 个调用点，其中
  `lifecycle_diagnostics`、`lifecycle_reminder_effect`、`supply_lifecycle`、
  `relist_freshness` 那些模块本来就不钉 `now`，让它们传一个自己也没有的
  `now` 进去，是把一个局部不一致摊成全局接口变更。
- **不改测试期望值为 59**。那等于把「同一行两个口径」这个真 bug 固化进
  测试，以后没人会再发现它。

`freshness_age_days` 本身不动。它是一个「现在几点」的属性，在别的模块里
用得对；错的只是本模块把它和一个钉了时刻的数并排印。

## 失败二：扫描规则把查询参数当成了归零

`test_refresh_count_is_never_reset_anywhere` 守的是本模块的前提：
`refresh_count` 是一个从不清零的累计计数器。它用字符串匹配扫全项目，找
`refresh_count=0`。

结论是真的——全项目没有任何一处把 `refresh_count` 归零。但扫描规则太粗，
误报了两个文件。用 AST 扫一遍，写 `refresh_count=0` 的只有三类：

| 位置 | 性质 |
| --- | --- |
| `relist_freshness.py` 的 `filter(refresh_count=0, ...)` | ORM 查询参数，找「从没刷新过的商品」 |
| `test_refresh_quota_drift.py` 的夹具 `create(..., refresh_count=0, ...)` | 建「从未刷新过」的夹具商品 |
| `test_relist_freshness.py` 的 `make_item(..., refresh_count=0)` | 同上 |

`relist_freshness.py` 那一处甚至不是灰色地带，它是「按零查询」的正当用法
的一个范例——那边的沉底判据写成 `created_at` 而不是 `freshness_age_days`，
正是因为判据里已经限定了 `refresh_count=0`：一次都没刷新过的商品
`last_refreshed_at` 必为空，两者等价。扫描规则应该显式容忍它。

### 修法

把字符串匹配换成 AST 判断，只认两种形态：

- `Assign`：目标是 `Attribute` 且属性名是 `refresh_count`，值是
  `Constant(0)`——即 `obj.refresh_count = 0`；
- `Call`：方法名是 `update` / `bulk_update` / `save` / `refresh_from_db`，
  且关键字参数 `refresh_count=0`。

`filter` / `exclude` / `create` / `get` / `get_or_create` 明确不算。扫描
范围不缩小——`os.walk('.')` 加既有的三个跳过目录原样保留，缩小范围会让
这条测试变弱。

这条测试守的东西不变：真的 `.update(refresh_count=0)` 出现时它照样红，
而那时该做的是把模块改掉，不是把这个测试删掉。

## 修完之后要守住的东西

两条测试都仍然能在真 bug 出现时红：

- 有人给 `age_days` 换成另一个参考时刻，`[60, 60, 60]` 会变，因为它和
  `since_refresh_days` 从此不再同源；
- 有人加了按月清零，AST 扫描会认出来，无论它写在 `update`、`bulk_update`
  还是直接赋值上。

## 不做什么

- 不动 `freshness_age_days` 本身。
- 不改 `refresh_quota_drift` 的任何口径，不改任何阈值、常量、文案。
- 不给任何人按月清零。本模块从头到尾只报告。
- 不缩小扫描范围，不给扫描规则加白名单文件——`relist_freshness.py` 被
  排除是因为它的写法在 AST 上不是归零，不是因为它被点名放过。

