# 拾光校园 · Campus Glimmer

> 一个为校园生活设计的轻量级二手交易社区。让闲置流转，让好物遇见对的人。

![Django](https://img.shields.io/badge/Django-5.1%2B-0c4b33?style=flat-square&logo=django)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab?style=flat-square&logo=python)
![License](https://img.shields.io/badge/license-MIT-1d8a68?style=flat-square)

## 项目简介

拾光校园（Campus Glimmer）是一个面向高校场景的二手物品交易平台，围绕“发布闲置、发现好物、友好沟通、安心交易”设计。项目保留了 Django 的清晰结构，同时重新整理了视觉语言和核心交互，让它更像一个可以继续迭代的真实产品，而不只是课程作业。

## 这次升级了什么

- **全新产品视觉**：以森林绿、奶油白和暖橙为主色，重做导航、首页、商品卡片、详情页、表单和响应式布局。
- **心愿单**：登录后可以收藏 / 取消收藏商品，并在“我的心愿单”集中查看。
- **更好用的商品浏览**：支持关键词搜索、分类筛选、价格排序和分页。
- **更清晰的首页**：增加平台数据概览、分类入口、上新商品和更有识别度的品牌首屏。
- **商品详情增强**：图片缩略图切换、商品状态、卖家名片、同类商品和留言讨论集中展示。
- **发布流程优化**：统一表单样式，支持图片预览、最多 5 张商品图和编辑时删除图片。
- **工程整理**：增加环境变量配置、数据库索引、后台心愿单管理、基础测试和完整运行文档。

## 功能一览

### 用户与社区

- 注册、登录、退出
- 个人资料与头像编辑
- 商品留言
- 用户私信、会话列表、未读消息

### 商品交易

- 发布、编辑、删除商品
- 多图上传与预览
- 商品分类与成色信息
- 在售 / 已预订 / 已售出状态管理
- 关键词搜索、分类筛选、价格排序、分页
- 商品收藏 / 心愿单
- 卖家联系与相关商品推荐

### 管理后台

- 分类、商品、商品图片管理
- 用户资料、评论、私信管理
- 心愿单记录管理

## 技术栈

- **后端**：Python、Django
- **数据库**：SQLite（本地开发默认）
- **前端**：Django Templates、Bootstrap 5、Bootstrap Icons、原生 CSS / JavaScript
- **媒体**：Pillow

## 快速开始

### 1. 获取代码并创建虚拟环境

```bash
git clone https://github.com/strawberry-little-bear/campus_glimmer.git
cd campus_glimmer
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 初始化数据库

```bash
python manage.py migrate
```

如果希望快速生成演示分类、用户和商品：

```bash
python init_data.py
```

### 4. 创建管理员并启动

```bash
python manage.py createsuperuser
python manage.py runserver
```

打开 <http://127.0.0.1:8000/>，后台地址为 <http://127.0.0.1:8000/admin/>。

## 配置说明

复制 `.env.example` 为 `.env`，或在启动前设置以下环境变量：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | Django 密钥，生产环境必须替换 | 本地开发占位值 |
| `DJANGO_DEBUG` | 是否开启调试模式 | `1` |
| `DJANGO_ALLOWED_HOSTS` | 允许访问的域名，逗号分隔 | `127.0.0.1,localhost,testserver` |

## 目录结构

```text
campus_glimmer/
├── accounts/             # 用户注册、资料与认证
├── listings/             # 商品、分类、收藏与交易流程
├── chat_messages/        # 留言、私信与会话
├── campus_glimmer/        # Django 项目配置
├── templates/             # 页面模板
├── static/                # CSS、JavaScript、静态图片
├── media/                 # 用户上传的媒体文件
├── init_data.py           # 演示数据脚本
├── requirements.txt
└── manage.py
```

## 开发检查

```bash
python manage.py check
python manage.py test
python manage.py makemigrations --check --dry-run
```

## 后续可以继续扩展

- 校园 / 校区维度的交易范围
- 举报、审核与敏感词过滤
- 订单、预约和线下交付状态
- 商品浏览历史与推荐
- 更换 SQLite 为 PostgreSQL，并接入对象存储
- 使用 Docker 和 CI 自动完成测试与部署

## License

本项目采用 MIT License，欢迎基于它继续学习、改造和创造。
