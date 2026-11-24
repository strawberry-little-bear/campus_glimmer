# 减少动态效果契约设计

## 问题

样式表一万多行，总闸之外还躺着 46 处 `transition:`、3 处 `animation:` 和 3 个
`@keyframes`（写这份契约时的量）。动效在这个项目里是「顺带就有」的：一个 hover 变色补一句
`transition: .18s`，一个骨架屏加一段微光，一个未读提示加一个入场。每一处单独看都无害，
加起来却让「系统里关掉动效」这件事失去了意义——`prefers-reduced-motion` 不是偏好设置，
前庭功能障碍用户会因为它头晕恶心，而它恰好又是最容易被忘记的那一类可访问性。

治理之前仓库里已经有两条 `@media (prefers-reduced-motion: reduce)`，但都是按选择器点名
处理的：一条收进度步骤 `.progress-step.is-current > span` 的脉冲与过渡，并补一个静态
`box-shadow` 兜住当前步骤的高亮；一条收运营看板懒加载骨架的微光。点名法有一个结构性
漏洞：往后任何人新增一处 `transition` 或 `@keyframes`，都会悄悄落在覆盖之外。这类缺口在
页面上完全看不出来——开发机不打开「减少动态效果」就永远发现不了，只有真的在系统里关了
动效的用户会撞上。写这份契约时漏网的就有 `.unread-toast` 的一次性入场动画
`unread-toast-in`，以及几十处 hover 过渡。

## 本轮范围

把兜底从点名表改成总闸：一条 `*` 通配加 `!important` 的
`@media (prefers-reduced-motion: reduce)`，追加在样式表末尾（`code { color: #b02a6b; }`
之后），钉住四个声明——`animation-duration: 0.01ms`、`animation-iteration-count: 1`、
`transition-duration: 0.01ms` 和 `scroll-behavior: auto`。

原有的两条点名规则一条不删。总闸只负责压秒，不负责补视觉指示：进度步骤停下来之后还要
有一个静态 `box-shadow` 标出「当前在这一格」，骨架屏停下来之后还要有一层静态底色表示
「这里还在加载」。这两件事只有点名规则做得到。

## 为什么是 * 通配总闸而不是把点名表补全

把点名表补全当然也能让这一页变绿，但它每次都要求写样式的人记得「这里加了动效，要去
reduced-motion 里再添一行」。契约的意义正是把这件事从人的记性里拿走：`*` 让后来新增的
元素和伪元素默认就被收口，`!important` 让它能压过别人在别处写的高优先级过渡，追加在样式
表末尾则保证它在文档序上也是最后说话的。

反过来说，只留点名表还有第二个问题——它没有失败信号。少写一行的结果是「什么都没发生」，
不是「测试红了」。总闸加断言之后，任何人删掉它、或者把 `!important` 写漏，测试都会立刻
报错，错误信息里还带着「为什么不是 `transition: none`」的推理过程。

## 为什么压秒到 0.01ms 而不是写 transition: none

`transition: none` 和 `animation: none` 会把属性该变的都一起变没。这里想要的是「不要
动」，不是「不要变色」：hover 该换的颜色要换，只是不再有中间帧；`infinite` 的脉冲要停，
但不能把元素留在动画中途的半空状态。压到 0.01ms 时属性照变、循环次数钉成 1，视觉上就是
瞬时到位，语义上又没丢任何终态。

两处安全性都对着代码核过，不是推想：

- 全站没有 JS 监听 `transitionend` / `animationend`，压秒不会打断任何依赖动画结束事件的
  交互；
- `.unread-toast` 的基础态没有 `opacity: 0`，压秒后只是瞬间出现，不会变成看不见——否则
  「关掉动效」会直接把未读提示变成永不显示，那比不治理更糟。

`scroll-behavior: auto !important` 是给将来补平滑滚动留的兜底。现在全站只有一处无关的
`overscroll-behavior-x`，没有 `scroll-behavior: smooth`；将来谁加上平滑滚动，都会被这条
自动压掉，不必再回来改一遍。

## 判据

- 契约只钉「兜底存在且写对」，不试图枚举每一处动效——枚举出来的那一刻就开始腐烂；
- 总闸之外必须真的有动画或过渡声明在管辖区内，防空转：如果将来全场动效都被删干净了，
  总闸和这条断言要一起删，而不是留一个永远通过的假绿；
- 点名规则要连同它的静态兜底一起保住，删了它就是删掉了当前步骤的视觉指示；
- 不宣称「全站无障碍已完成」：键盘焦点顺序、窄屏布局与图片加载仍在逐页补齐，见
  [后续路线图](roadmap.md)。

## 不做什么

- 不删既有动效：桌面端的视觉方向一点不变，只是关掉动效的用户不再被晃到；
- 不做「减少动效」的站内开关：那是系统层的事，页面只负责响应系统设置；
- 不在测试里跑浏览器：动效契约是静态断言。真实浏览器验收已经由[文字对比度契约](contrast-contract-design.md)
  那一轮做过，动效只需要保证 CSS 声明在位、值写对；
- 不把样式表里每一条 transition 都抄进文档：数字会漂，判据不会。

## 验证

- `campus_glimmer/test_reduced_motion.py` 4 个测试全绿；
- 两种失败模式都做过反向验证：删掉整个总闸块，`test_reduced_motion_catch_all_exists`
  报错；把 `transition-duration: 0.01ms !important` 的 `!important` 去掉，
  `test_catch_all_pins_durations_and_stops_loops` 报 `Regex didn't match`；
- 全站 grep 确认没有 `transitionend` / `animationend` 监听、没有 `scroll-behavior: smooth`；
- 完整测试集、`manage.py check`、迁移一致性检查与 `git diff --check` 均通过。
