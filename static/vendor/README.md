# vendor · 本地化的第三方前端依赖

这几个文件是从官方 CDN 原样下载的产物，未做任何修改（Bootstrap Icons 的 `url()` 仍指向同级 `fonts/` 目录，保持相对路径即可被 Django 静态文件系统解析）。

| 文件 | 来源 | 版本 | 体积 | 许可证 |
| --- | --- | --- | --- | --- |
| `bootstrap.min.css` | [getbootstrap.com](https://getbootstrap.com/docs/5.3/getting-started/download/) | 5.3.3 | 227 KB | MIT |
| `bootstrap.bundle.min.js` | 同上（含 Popper） | 5.3.3 | 79 KB | MIT |
| `bootstrap-icons.min.css` | [icons.getbootstrap.com](https://icons.getbootstrap.com/) | 1.11.3 | 84 KB | MIT |
| `fonts/bootstrap-icons.woff2` | 同上 | 1.11.3 | 127 KB | MIT |
| `fonts/bootstrap-icons.woff` | 同上 | 1.11.3 | 172 KB | MIT |

## 为什么要 vendor 而不是走 CDN

此前 `templates/base.html` 直接从 `unpkg.com` 和 `fonts.googleapis.com` 拉这几类资源。实测（本地 Chrome 无头、1424px、冷缓存）它们是页面上仅有的几个跨域请求，且**全部阻塞渲染**：

```
fonts.googleapis.com/css2?family=Noto+Sans+SC     224ms  render-blocking
unpkg.com/bootstrap@5.3.3/dist/css/...            169ms  render-blocking
unpkg.com/bootstrap-icons@1.11.3/font/...         176ms  render-blocking
unpkg.com/bootstrap@5.3.3/dist/js/...             176ms  blocking
```

更关键的是可用性：Google Fonts 与 unpkg 在国内网络下经常不可达或需要数秒重试，一旦失败，页面会退化成「无样式的 HTML」——对以校园用户为主的项目这是不可接受的风险。本地化后首屏不再依赖任何第三方域名，离线也能完整渲染。

## 升级方式

替换对应文件后，同步更新上表版本号与 `templates/base.html` 中的引用即可。没有构建链，不需要 `npm install`：

```powershell
Invoke-WebRequest https://unpkg.com/bootstrap@5.3.3/dist/css/bootstrap.min.css -OutFile static/vendor/bootstrap.min.css
```

> 注意：生产环境使用 Django 的 Manifest 静态文件存储时，`collectstatic` 会重写 CSS 内的 `url()` 并给字体文件加哈希后缀，这是预期行为，不要手动改 `bootstrap-icons.min.css` 里的相对路径。

## 正文字体

正文不再使用 Google Fonts 的 Noto Sans SC，改为 `static/css/style.css` 中的中文优先系统字体栈。原因：Noto Sans SC 通过 Google Fonts 分发时被拆成 101 个 unicode-range 分片，全量本地化约 1.6 MB，对当前仓库体积不划算；而系统栈零请求、零 FOUT，且各平台都有完整的中文字形覆盖（macOS 苹方、Windows 微软雅黑、Android Noto Sans CJK），不会出现缺字方框。
