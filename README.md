<div align="center">

# Nehchat Agent · 存档版

**10 个独立存档位 · 单 / 双模型 Role-Play · SSE 流式对话 · 自建 OpenAI 兼容模型目录**

[简体中文](./README.md) · [English](./README.en.md)

![Python](https://img.shields.io/badge/Python-3.14+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-latest-009688?logo=fastapi&logoColor=white)
![MySQL](https://img.shields.io/badge/MySQL-8-4479A1?logo=mysql&logoColor=white)
![Vite](https://img.shields.io/badge/Vite-6-646CFF?logo=vite&logoColor=white)
![Streaming](https://img.shields.io/badge/Streaming-SSE-f97316)

</div>

---

一个开箱即用的 AI 对话应用：在「模型配置」中自行添加任意 OpenAI 兼容供应商与模型，通过 10 个存档位管理多段独立对话。支持单模型直聊与双模型角色扮演（两模型依次回复、互相接力），对话全程 SSE 流式输出、落库 MySQL，并配备多用户账号（数据按用户隔离）、主题换肤、自定义背景图、Markdown 渲染与完整备份恢复。

前端 Vite + 原生 JS，后端 FastAPI + MySQL，一键脚本启动；内置账号登录与数据隔离，可通过邀请码开放注册，公网隧道访问还可叠加 Basic Auth 外层门禁。

## 目录

- [功能特性](#-功能特性)
- [系统架构](#️-系统架构)
- [快速开始](#-快速开始)
- [配置项](#️-配置项)
- [供应商与模型](#️-供应商与模型)
- [双模型 Role-Play](#-双模型-role-play)
- [主题与背景](#-主题与背景)
- [目录结构](#-目录结构)
- [API 一览](#-api-一览)
- [SSE 事件流](#-sse-事件流)
- [开发指南](#️-开发指南)
- [常见问题](#-常见问题)
- [安全须知与许可](#-安全须知与许可)

## ✨ 功能特性

### 👥 多用户与账号

- **首次初始化**：首次打开页面创建管理员账号（仅限本机完成），已有数据自动归入该账号
- **邀请码注册**：`.env` 设置 `INVITE_CODE` 后开放注册，未设置则仅已有账号可用
- **数据隔离**：存档、消息、供应商与模型目录全部按用户隔离，互不可见
- **安全存储**：密码使用 scrypt 加盐哈希；HttpOnly Cookie 会话（30 天），支持注销
- **可选外层门禁**：公网隧道场景可再设置 `WEB_USER` / `WEB_PASSWORD` 叠加 HTTP Basic Auth

### 🗂️ 存档系统

- **10 个独立存档位**：每个存档独享 `model / system_prompt / params / dual_config`，互不干扰，可并行多段对话
- **6 步创建向导**：模式 → 模型 → 参数 → 提示词 → 模型2 → 模型2参数；目录为空时不可创建
- **存档内热更换模型**：`PATCH /api/slots/{i}/config` 原子更新模型/提示词/参数，不影响历史消息；双模型两侧完全独立
- **自动标题**：首次对话后按用户首条消息自动生成标题，侧栏可原地重命名

### 💬 对话能力

- **SSE 流式输出**：`POST /api/chat` 与 `POST /api/slots/{i}/chat/continue`，实时逐字渲染，支持取消（`■`）与断点回滚
- **继续回复（⏩）**：单模型将续写内容合并回最后一条回复；双模型相当于用户留空，两位角色各再回复一轮
- **消息精细操作**：编辑（保存后自动重发）、按 ID 精确删除、重新生成（`↻`，删除该轮及其后消息并重发）、清空对话
- **上下文窗口**：每次传给模型最近 100 条消息，避免长对话超限
- **Markdown 渲染**：`marked` + `highlight.js` 代码高亮，代码块一键复制

### 🏷️ 模型目录

- **自建供应商清单**：右上角「模型配置」添加任意 OpenAI 兼容端点（基础 URL、密钥或环境变量、model-id），存档只从目录中选择
- **连通性测试**：每个已保存模型旁可发送 `hello` 探测，返回延迟与回复预览
- **密钥管理**：密钥挂在供应商上，可明文存储或从环境变量读取；`GET /api/env-check` 检查各供应商密钥是否就绪
- **引用保护**：被存档引用的供应商/模型不可删除，导入备份前须先在目录中建好对应模型

### 🎨 个性化

- **5 套配色主题**：宇宙紫蓝（默认）/ 翡翠绿 / 暖橙日落 / 樱花粉 / 深海青，一键切换
- **明暗模式**：深色 / 浅色分段切换，首次访问跟随系统偏好，localStorage 持久化
- **自定义背景图**：上传 / 选择 / 删除当前账号的背景，支持透明度与模糊度调节（≤10MB，jpg/png/webp/gif）

### 🔒 可靠性

- **并发保护**：内存 `asyncio.Lock` + MySQL `GET_LOCK(ai_chat_slot_*)` 双层锁，冲突时返回 `slot_busy` 友好提示
- **智能重试**：4xx 不重试；5xx / 超时 / 网络错误指数退避重试；流开始后中断不重试（防内容重复）
- **失败回滚**：流中断或异常时自动回滚已落库消息（`delete_messages_from`），双模型中模型2失败时保留模型1已完成的回复
- **完整备份**：导出可完整恢复的 JSON 备份（version 2）与 Markdown 对话导出，导入到任意空存档位

## 🏗️ 系统架构

```
┌────────────────────── 浏览器（Vite 构建的静态前端）──────────────────────┐
│  宫格存档视图 ⇄ 聊天视图 · 6步向导 · 模型配置 · 主题/背景弹层 · 帮助       │
│  api.js: fetch + 指数退避          chat.js: SSE 流解析与事件分派          │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │ HTTP / SSE  (127.0.0.1:8000)
┌──────────────────────────────────▼──────────────────────────────────────┐
│ FastAPI（main.py lifespan 初始化全局单例）                               │
│   routes/  slots · chat · models · catalog · backgrounds                │
│   helpers/ resolve_slot · get_runtime（模型key → 运行时配置）· error     │
│   clients/ AIClient — AsyncOpenAI 流式调用 + 重试                        │
│   state/   AIClient / SlotManager 单例                                  │
│   session_manager/ SlotManager — 事务模板 · 连接池 · GET_LOCK            │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │ pymysql + DBUtils 连接池
                            ┌──────▼──────┐
                            │   MySQL 8   │  slots · messages
                            │  (自动建库)  │  providers · catalog_models
                            └─────────────┘
```

## 🚀 快速开始

### 环境要求

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.14+ | 后端运行 |
| MySQL | 8.x | 数据存储（需已启动服务） |
| Node.js | 18+ | 仅前端构建 / 开发时需要 |

### 启动步骤（Windows）

1. **配置数据库连接**：在项目根目录创建 `.env`（参考下文[配置项](#️-配置项)），`MYSQL_PASSWORD` 必填
2. **首次运行 / 依赖变化后**：双击 `重置启动.bat` —— 安装后端依赖、构建前端、以 `--reload` 启动服务并打开浏览器
3. **日常启动**：双击 `快速启动.bat` —— 检测 8000 端口占用，直接启动后端并自动打开浏览器
4. **首次访问**：页面会要求创建管理员账号（仅本机可完成），历史数据自动归入该账号；如需邀请朋友注册，在 `.env` 设置 `INVITE_CODE`

> 数据库 `ai_chat` 与全部数据表（slots / messages / providers / catalog_models）在服务首次启动时**自动创建**，含旧表结构自动迁移，无需手动执行 SQL。

### 手动启动

```bash
cd backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000
# 访问 http://localhost:8000
```

### 公网访问（可选）

双击 `公网隧道.bat`：自动启动后端并通过 Cloudflare 快速隧道（cloudflared）生成 `https://*.trycloudflare.com` 公网地址；服务仍在本机运行，关闭窗口即断。所有数据默认需要账号登录，如需额外一层门禁可在 `.env` 中设置 `WEB_USER` / `WEB_PASSWORD` 叠加 Basic Auth。

## ⚙️ 配置项

通过项目根 `.env` 或系统环境变量设置：

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `MYSQL_HOST` | 否 | `localhost` | MySQL 地址 |
| `MYSQL_PORT` | 否 | `3306` | MySQL 端口 |
| `MYSQL_USER` | 否 | `root` | MySQL 用户 |
| `MYSQL_PASSWORD` | **是** | — | 未设置时服务拒绝启动 |
| `MYSQL_DATABASE` | 否 | `ai_chat` | 库名（不存在自动创建） |
| `ALLOWED_ORIGINS` | 否 | `http://localhost:5173,...` | CORS 白名单，逗号分隔 |
| `INVITE_CODE` | 否 | — | 注册邀请码；设置后开放注册，不设置则仅已有账号可用 |
| `WEB_USER` | 否 | — | 可选的 Basic Auth 外层门禁用户名（与内置账号登录叠加） |
| `WEB_PASSWORD` | 否 | — | 可选的 Basic Auth 外层门禁密码 |
| `UVICORN_HOST` | 否 | `127.0.0.1` | 监听地址（`python main.py` 时生效） |
| `UVICORN_RELOAD` | 否 | `0` | 热重载开关（`python main.py` 时生效） |

> 供应商密钥环境变量：在「模型配置」中为供应商勾选「从环境变量读取」后，把变量名配成你在此设置的名字（如 `MYPROVIDER_API_KEY=sk-xxxx`），新进程启动后生效。

**生成参数说明**：创建存档时可预设 `temperature / top_p / presence_penalty / frequency_penalty / num_predict` 等参数；实际调用仅发送 OpenAI 标准参数（`num_predict` 折算为 `max_tokens`，上限 8192），其余参数（`min_p / top_k / repeat_penalty / num_ctx`）仅作记录，不会发送给不兼容的接口。

## 🏷️ 供应商与模型

应用不内置任何供应商，打开右上角「模型配置」自行添加：

1. **提供商 ID**：小写字母/数字/连字符/下划线，创建后不可修改
2. **显示名称 + 基础 URL**：任意 OpenAI 兼容端点，如 `https://api.example.com/v1`、本地 Ollama `http://127.0.0.1:11434/v1`
3. **API 密钥**：可留空（本地无鉴权服务），或勾选「从环境变量读取」并填写变量名
4. **添加模型**：`model-id`（发给接口的真实名称）+ 显示名称，可添加多条
5. **测试**：保存后点模型旁「测试」，发送 `hello` 验证连通性

存档引用的模型 key 形如 `{provider_slug}:{model_id}`；请求运行时由 `get_runtime` 解析为真实的 `base_url + model_id + api_key`。

## 🎭 双模型 Role-Play

双模型存档由 `dual_config` 驱动，两个模型各自拥有独立的模型、提示词、参数与角色名：

| 配置 | 取值 | 行为 |
|---|---|---|
| `response_mode` | `both` / `model1` / `model2` | 同时回复 / 仅模型1 / 仅模型2，侧栏可随时切换 |
| `first_model` | `model1` / `model2` | 本轮谁先回复 |
| `pass_mode` | `user` / `assistant` | 模型1 的回复如何传给模型2 |

**接力机制**（模型2 如何看到模型1 的回复）：

- `pass_mode: user`（默认）：模型1 回复以 `[角色名 的回复]` 合并进用户消息，避免连续两条 user
- `pass_mode: assistant`：模型1 回复以独立 `assistant` 消息（`角色名: 内容`）传入

**流式事件顺序**：`model_start(role) → chunk* → model_done(role, message_id) → … → done(user_message_id, message_ids)`，前端据此渲染带角色标签（🎭 / 🌟）的气泡。

## 🎨 主题与背景

- 页面右上角主题按钮（宫格页与聊天页均有）打开主题弹层：顶部明暗分段切换，下方 5 套配色主题
- 弹层底部「自定义背景」区：上传图片（≤10MB）或选择已有图片，实时调节透明度（0–100%）与模糊度（0–20px）
- 用户上传存放于 `backend/backgrounds/<user_id>/`，需登录后访问 `/backgrounds/{name}`；也可在 `backend/backgrounds/` 根目录放入共享图片（只读）
- 所有偏好（主题 / 明暗 / 背景）保存在浏览器 localStorage，首次渲染前内联脚本预应用，无闪烁

## 📂 目录结构

```
AI_project/
├─ backend/
│  ├─ main.py             # FastAPI 入口：lifespan 初始化、注册路由、托管前端
│  ├─ config.py           # MySQL / 槽位 / 上下文窗口 / 背景图 / 默认参数
│  ├─ clients.py          # AIClient：流式调用、hello 测试、重试与错误映射
│  ├─ auth.py             # 可选 Basic Auth 中间件 + 登录会话依赖（current_user）
│  ├─ accounts.py         # 账号与会话：scrypt 密码哈希、注册/登录、旧数据接管
│  ├─ helpers.py          # error / resolve_slot / get_runtime / 密钥解析
│  ├─ models.py           # Pydantic 请求/响应模型
│  ├─ session_manager.py  # SlotManager：建库建表、CRUD、目录、锁与事务
│  ├─ state.py            # 全局单例
│  ├─ routes/
│  │  ├─ auth.py          # 初始化 / 邀请码注册 / 登录注销 / 当前用户
│  │  ├─ slots.py         # 存档 CRUD、消息操作、配置更新、备份导入导出
│  │  ├─ chat.py          # /api/chat 与 continue 的 SSE 流（单/双模型）
│  │  ├─ models.py        # 模型列表 / env-check / 默认参数
│  │  ├─ catalog.py       # 供应商与目录模型 CRUD、hello 测试
│  │  └─ backgrounds.py   # 背景图上传 / 列表 / 删除
│  └─ backgrounds/        # 自定义背景图目录（运行时生成）
├─ frontend/
│  ├─ index.html          # 宫格视图 + 聊天视图 + 向导 + 各弹层
│  ├─ js/
│  │  ├─ main.js          # 入口：事件绑定与模块装配
│  │  ├─ api.js           # apiFetch：指数退避（4xx 不重试）
│  │  ├─ state.js         # 全局状态
│  │  ├─ chat.js          # SSE 解析、发送/继续/取消/重发/编辑
│  │  ├─ auth.js          # 登录 / 注册 / 首次初始化视图与 401 处理
│  │  ├─ catalog.js       # 模型配置弹层（供应商/模型增删改测）
│  │  ├─ modals.js        # 6 步创建向导 + 编辑复用
│  │  ├─ theme.js         # 主题与明暗模式
│  │  ├─ background.js    # 自定义背景管理
│  │  └─ ui / markdown / confirm / toast / utils
│  ├─ css/                # variables / themes / base / chat / modals / ...
│  └─ vite.config.js      # dev 5173 代理 /api 与 /backgrounds → 8000
├─ 快速启动.bat            # 日常启动：端口检测 + 起服务 + 开浏览器
├─ 重置启动.bat            # 装依赖 + 构建前端 + --reload 启动
├─ 公网隧道.bat            # 公网隧道：cloudflared + 后端 + 鉴权检测
└─ .env                   # 本地环境变量（已被 .gitignore 忽略）
```

## 🔌 API 一览

统一错误格式 `{code, message, detail}`，前端映射为中文 Toast / 内联卡片。除 `/api/auth/*` 与 `GET /api/auth/status` 外，所有 `/api` 接口均需登录（Cookie 会话）。

### 账号

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/auth/status` | 是否已初始化 / 是否开放注册（无需登录） |
| `POST` | `/api/auth/setup` | 首次初始化管理员（仅限本机，自动接管旧数据） |
| `POST` | `/api/auth/register` | 邀请码注册 |
| `POST` | `/api/auth/login` | 登录（写入会话 Cookie） |
| `POST` | `/api/auth/logout` | 注销当前会话 |
| `GET` | `/api/auth/me` | 当前登录用户 |

### 存档与消息

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/slots` | 10 个存档位概览（`message_count` / `dual_enabled` 等） |
| `POST` | `/api/slots/{i}` | 创建存档（`CreateSlotRequest`，含双模型分支） |
| `DELETE` | `/api/slots/{i}` | 删除存档（消息 `CASCADE` 级联删除） |
| `GET` | `/api/slots/{i}/chat` | 存档详情 + `history[{id, role, content, source}]` |
| `POST` | `/api/slots/{i}/chat/clear` | 清空对话 |
| `DELETE` | `/api/slots/{i}/chat/messages` | 删消息（优先 `from_id`，回退 `from_index/to_index`） |
| `PATCH` | `/api/slots/{i}/chat/messages` | 编辑消息（`message_id` 优先） |
| `PATCH` | `/api/slots/{i}/title` | 修改标题 |
| `PATCH` | `/api/slots/{i}/config` | **模型热更换**（原子更新，双模型两侧独立） |
| `PATCH` | `/api/slots/{i}/dual-toggle` | 切换 `response_mode / first_model` |
| `PATCH` | `/api/slots/{i}/api-key` | 已废弃（410，密钥改在模型配置中管理） |

### 对话流

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/chat` | 流式对话 SSE（单/双分流，失败自动回滚） |
| `POST` | `/api/slots/{i}/chat/continue` | 继续回复（单模型合并续写、双模型各回复一轮） |
| `POST` | `/api/slots/{i}/chat/cancel` | 取消当前流式回复并释放锁 |

### 模型目录

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/models` | 目录中的全部模型（含供应商信息） |
| `GET` | `/api/env-check` | 各供应商运行时密钥是否可解析 |
| `GET` | `/api/default-params` | 默认生成参数 |
| `GET` / `POST` | `/api/providers` | 供应商列表（密钥仅掩码）/ 创建 |
| `PATCH` / `DELETE` | `/api/providers/{id}` | 更新（不可改 slug）/ 删除（无存档引用时） |
| `POST` | `/api/providers/{id}/models` | 添加模型 |
| `PATCH` / `DELETE` | `/api/providers/{id}/models/{mid}` | 更新 / 删除模型 |
| `POST` | `/api/providers/{id}/models/{mid}/test` | 非流式 `hello` 连通性测试 |

### 备份与背景

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/slots/{i}/chat/export` | 导出 Markdown 对话数据 |
| `GET` | `/api/slots/{i}/backup` | 导出完整 JSON 备份（version 2） |
| `POST` | `/api/slots/{i}/backup` | 导入备份到空存档位 |
| `GET` / `POST` | `/api/backgrounds` | 背景图列表 / 上传（≤10MB） |
| `DELETE` | `/api/backgrounds/{name}` | 删除背景图 |

## 📡 SSE 事件流

`Content-Type: text/event-stream`，每帧 `data: {json}`：

| 事件 | 载荷 | 说明 |
|---|---|---|
| `model_start` | `role, name, icon` | 某个模型开始生成（双模型逐个触发） |
| `chunk` | `content, role?` | 增量文本片段 |
| `model_done` | `role, name, icon, message_id` | 单个模型生成完毕并已落库 |
| `continue_start` | `message_id` | 单模型继续回复开始（追加到该消息） |
| `done` | `user_message_id, message_ids / assistant_message_id` | 整轮完成 |
| `error` | `code, content, key_target?` | 出错（`slot_busy` / `auth_failed` / `rate_limited` / `stream_interrupted` 等），携带回滚定位 ID |

## 🛠️ 开发指南

```bash
# 前端开发（dev server 代理 /api 与 /backgrounds → 8000）
cd frontend
npm install
npm run dev          # http://localhost:5173

# 后端开发
cd backend
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload

# 生产构建（backend 启动时优先托管 frontend/dist）
cd frontend
npm run build
```

数据表结构变更通过 `_init_tables` 的列检测自动迁移（如 `slots.params`、`messages.source`、`slots.model VARCHAR(192)`），升级代码后直接重启即可。

## ❓ 常见问题

- **启动报 MySQL 连接失败**：确认 `MYSQL_PASSWORD` 已设置、MySQL 服务已启动、主机端口正确；服务会自动 `CREATE DATABASE`，但需要用户具备建库权限
- **创建存档时没有可选模型**：先在右上角「模型配置」添加供应商与模型
- **本地 Ollama**：基础 URL 填 `http://127.0.0.1:11434/v1`，密钥可留空；需服务常驻且已 `ollama pull`
- **对话报 401 / 认证失败**：到「模型配置」检查该供应商密钥或环境变量；可用模型旁「测试」快速排查
- **流式中断**：已开始后中断不重试（防重复），提示"回复生成中途中断，请重试"；可点 `⏩继续` 或重新发送
- **提示存档正在生成**：同一存档位有并发请求，等待上一轮完成即可；双层锁保证不会串写
- **端口 8000 被占用**：`快速启动.bat` 检测到占用时直接打开现有服务，不会重复启动

## 🔒 安全须知与许可

- 项目**内置账号登录与数据隔离**，服务默认只监听 `127.0.0.1`；公网暴露时建议再设置 `WEB_USER` / `WEB_PASSWORD` 叠加一层 Basic Auth 门禁
- 密码以 scrypt 加盐哈希存储；API 密钥明文存储于 MySQL（或经环境变量注入），请自行做好机器安全
- 未声明开源许可证，仅供本地个人使用

---

<div align="center">

> 相关文档：[后端运行说明](./backend/README.md)

</div>
