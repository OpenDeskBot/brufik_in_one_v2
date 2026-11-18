# deskbot-server 架构

本项目按 HTTP 服务工程 SOP v0.4-draft 绑定 Python 3.11、FastAPI、SQLite、SQLAlchemy Core 及原生浏览器页面。整改范围、验收证据和旧测试问题见 [SOP 实施记录](SOP_COMPLIANCE.md)。

## 分层与目录

| 目录 | 职责 |
| --- | --- |
| `controller/`、`web/blueprints/` | HTTP/WS 路由、访问声明、参数接入和响应转换；后者保留既有模块名称 |
| `controller/decorators/` | 权限、异常转换、请求标识与最终状态统计 |
| `model/` | 对象与字段转换、共享错误；`db/models.py` 保留数据库表声明及记录类型 |
| `service/` | 用户、设备、记忆、会话、模型选择、语音与主动交互规则 |
| `dao/`、`dao/decorators.py` | 具名直接 SQL、记录映射、统一访问异常和事务参与 |
| `client/` | ASR/TTS/LLM、人脸和声纹的网络协议实现；不自行选择用户设备的 provider |
| `schedule/` | 稳定任务声明、显式注册、公共执行记录、重入控制和唯一执行者锁 |
| `config/` | 文件和环境变量合并、校验、启动快照与显式配置更新 |
| `public/site/`、`public/admin/`、`public/shared/` | 官网、控制台和共用浏览器资源 |
| `ports/` | ASR、LLM、TTS、下行等 Protocol |
| `infrastructure/` | 应用装配、本机模型进程监督、烧录和 WebSocket 适配 |
| `pb/`、`vision/` | 机器人播放协议、表情、口型和视觉辅助 |

主链路：Controller / Schedule → Service → DAO / Client。多步数据库操作由 Service 创建事务并显式传给 DAO，只有事务创建者提交或回滚；无事务的独立 DAO 操作自己释放会话。SQLAlchemy 用于驱动、表声明和记录构造，业务数据访问不使用 ORM 查询与持久化。

## HTTP 与权限

- 每个路由显式 `@access(...)`；公开、普通用户和开发者分别声明。设备和订阅 WebSocket 有独立访问类别。
- 启动通过显式模块列表注册，缺访问声明、重复方法与路径立即失败；自定义 WS 别名也经过同一校验。
- HTTP 执行顺序为统计 → 异常转换 → 身份与权限 → 接口。同步入口由 FastAPI 线程池执行；异步路径的同步数据访问使用 `run_blocking`。
- 成功、权限拒绝、422、404/405 和异常都带 `X-Request-ID`，日志记录同一 ID、方法、路由模板、最终状态和耗时。
- 主服务只有一个公开 `GET /health`，固定返回 `200 application/json {"status":"ok"}`，不探测模型或数据库。
- 普通 JSON 保留旧项目的 `ok/error` 契约。未预期错误返回固定消息，内部原因留在日志。
- Web 会话或签名 debug token 必须对应有效用户；设备访问仍检查归属。开发者 API 不因前端隐藏菜单而放松权限。

## 浏览器页面

页面 URL 保留 `/`、`/home`、`/expr` 等既有路径。每个已声明页面只返回静态入口 HTML；页面加载后读取 `/api/pages<页面路径>` 的 JSON。页面资源允许匿名加载，数据接口仍校验相应用户或开发者身份。

`web/pages.py` 只组织 JSON 上下文及静态文件映射，不执行模板。27 份浏览器视图源码由 `tools/build_public.py` 打包为 `public/shared/views.json`，浏览器用固定版本的 Nunjucks 渲染，再启动既有 Vue 页面。运行服务不需要 Node 或 Jinja2。Node 22 用于页面回归与可选浏览器验收。

静态挂载映射：`/static/` → `public/shared/`；官网入口 → `public/site/index.html`；已声明控制台页面 → `public/admin/index.html`。没有全局 HTML 回退；未知 API、缺失资源和未声明页面均保持 404。

## 语音与客户端

设备采音 → `/asr_chat` → VAD → ASR Client → 对话 Service / 工具 → TTS Client → pb → 设备。

设备 provider、历史与提示词组织位于 Service。无状态 ASR/TTS Client 按配置复用，持久连接在应用正常停止时关闭；网络失败不通过换用默认客户端进行隐式重发。豆包 TTS 默认单次尝试。设备配置和控制台显式更新是运行中配置更新的已启用范围；普通文件修改需要重启。

## 调度与生命周期

`reminders.poll` 在 `schedule/reminders.py` 声明，应用装配显式注册。依赖和路由校验完成后才随应用启动。

- 默认 60 秒轮询；同任务重入跳过，失败不立即重试。
- 每次成功、失败、跳过均记录任务和触发 ID、计划/实际时间、状态、耗时及原因。
- 正常关闭先停新触发、等待已执行任务完成，再关闭设备和模型客户端。
- 单机同一 SQLite 库通过文件锁只允许一个调度执行者。多实例部署须指定唯一启用实例，其余设置 `DESKBOT_SCHEDULER_ENABLED=0`；本项目的设备状态仍按单进程部署绑定。

## 配置

`config.load_config()` 在启动时读取 YAML、合并环境变量并校验；`configure_runtime()` 固定运行快照。`AppSettings` 仅转换已取得的数据，不自行读取环境。设备级 provider、参数和记忆属于业务数据；控制台明确保存配置后才更新相应缓存/运行依赖。

## 开发检查

```bash
cd service
python tools/build_public.py
ruff check src
python -m compileall -q src
pytest -q tests/test_sop_contract.py tests/test_controller_auth.py
```

完整本地回归和实际浏览器步骤见 [SOP 实施记录](SOP_COMPLIANCE.md)。

## 设备数据归属

账号只通过 `devices.owner_user_id → users.id` 持有设备。记忆、提醒、人脸、声纹、对话、用量、剧本运行状态均归设备；识别到的人名只是该设备档案的内容，不能用人名或账号 ID 在设备间共享档案。米家授权、媒体和人物社交记录保存在 `data/{device_id}/`。共享模型服务、素材库、剧本定义和默认模板不代表某个账号的设备数据。

数据库开启 SQLite 外键：7 张设备业务表的 `device_id` 必须引用已登记的设备；消息通过会话归属设备；提醒关联会话时使用 `(session_id, device_id)` 复合外键，不能引用别的设备的会话。设备编号更新会同步更新业务表；存在业务数据时禁止直接删除设备。删除账号只解除设备所有权。

设备可以先连接、产生数据，再绑定账号。解绑只清空所有者及绑定时间，保留设备身份、设置、文件和业务数据；重新绑定只改变访问权限。清空内容须使用独立的清除数据接口。控制台及调试订阅都校验当前设备所有权，开发者身份不免除设备归属校验。记忆和档案等业务接口缺少设备范围时拒绝执行，按资源 ID 查询、修改和删除的 SQL 同时限定设备。

旧库在启动时由 `db/device_ownership.py` 迁移，部署顺序、数据保留与验证记录见 [设备归属整改记录](device-ownership.md)。

## 文件记忆

`memory_service` 编排每日证据、人物归属、版本、修订和遗忘；`memory_file_store` 负责设备目录中的文件锁、写入、原子替换和事务恢复；`model/memory.py` 定义分块、格式和时间规则。记忆运行时不访问 SQLite，旧 `device_memory` 表只用于一次性导出及历史结构兼容。

实时连接的 `MemorySession` 独立消费最终转写和执行回执，不复用会丢弃旧事件的网页订阅队列。后台增量整理只采用本轮人物与真实来源；复杂整理经 Agent 工具执行。Flecket 每轮及工具续答读取最新人物文件，固定人设中不保存人物正文。遗忘使旧写入代次失效并重建连接，避免缓存或迟到任务恢复已删除信息。文件布局、工具、接口、迁移与验证见 [SERVER.md 的文件记忆](SERVER.md#文件记忆)。
