# 搜索建议 Combobox 契约设计

全局搜索框的提示列表以前只有一半的可访问性契约：`role="listbox"` 和
`role="option"` 早就存在，JS 也会在拉到建议后翻转 `aria-expanded`，
但输入框本身没有 `role="combobox"`。按 ARIA 1.2，纯 textbox 不支持
`aria-expanded`，读屏会把这个状态整个忽略；方向键在候选里走到第几项，
也只有看得见的人知道。

这份文档记录现在的口径，后续改搜索框时按它来，不要倒退。

## 渲染态（模板负责）

`templates/base.html` 里的全局搜索输入框必须自带：

- `role="combobox"`：声明这是一个组合框，而不是普通文本框；
- `aria-autocomplete="list"`：提示来自一组候选，而不是输入法式的内联补全；
- `aria-expanded`：默认 `false`，由 JS 在展开/收起时翻转；
- `aria-controls` 指向真正的 `role="listbox"` 容器，且容器带 `id`。

没有 JS 时这些属性只是静态声明，页面照常提交表单，属于可接受的退化；
有 JS 时它们是读屏播报状态的前提。

## 行为态（JS 负责）

建议逻辑收在 `static/js/main.js` 顶层的
`createSearchSuggestController` 工厂里，`DOMContentLoaded` 只做接线。
工厂内部维护一份单一状态机：

- 每个选项渲染时带上 `id`（`<listbox id>-option-<序号>`）、
  `role="option"` 和初始 `aria-selected="false"`；
- 方向键移动、收起、重新渲染，全部经过统一的 `setActive`：
  它同时维护选项的 `is-active` class、选项的 `aria-selected`，以及
  输入框上的 `aria-activedescendant`；
- 收起建议时 `aria-expanded` 归位、`aria-activedescendant` 摘除、
  所有选项取消选中，三条一起做，不允许散落在各分支各改各的；
- Enter 激活当前项、点击选项写回输入框并提交、输入防抖与
  `AbortController` 取消旧请求，行为与重构前一致。

规定"只有 `setActive` 能动激活态"的原因很直接：读屏要的
「当前选中哪一项」来自 `aria-activedescendant` 和 `aria-selected`
的一致性。这两个状态一旦散落在键盘、点击、渲染三条路径里各自维护，
漏掉任何一处，用户听到的和看到的就会分叉。

## 测试口径

`listings/test_search_suggest_combobox.py` 从两层锁住契约：

- 渲染态测试请求真实首页，断言输入框的角色、自动补全声明和
  `aria-controls` 指向 listbox；
- 行为态测试沿用运营看板的 Node 探针套路：从 `main.js` 抽出工厂，
  配假 DOM 跑真实逻辑，覆盖方向键循环、Escape 收起、Enter 激活、
  点击写回、防抖和请求取消，另用源码断言确认键盘路径只经过
  `setActive`、渲染时给选项打上 ARIA 标记。

环境里没有 node 时行为探针跳过，渲染态契约在任何环境都生效。

## 边界之外

这次只补搜索建议框的 combobox 契约。排查过程中确认过几类常见问题
在本仓库不存在或无需处理：全站没有缺 `noopener` 的 `target="_blank"`，
没有缺 `title` 的 iframe，图标按钮都有可访问名，表单按钮的
`type` 均合法；`accounts` 表单里仅剩的两个 email 字段浏览器能可靠
推断自动补全，暂不为凑属性而改。
