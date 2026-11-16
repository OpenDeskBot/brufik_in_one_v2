# 服务端 SOP 实施与验收记录

日期：2026-11-10。依据：HTTP 服务工程 SOP v0.4-draft（技能版本 0.1.0）。范围为 `service/`、控制台静态资源及服务端 CI；硬件固件不在本次整改范围。

本记录区分本次整改项的行为验收、旧项目兼容绑定及全库历史测试问题。结构说明见 [ARCHITECTURE.md](ARCHITECTURE.md)，机器可读结果见 [sop-verification-2026-11-10.json](sop-verification-2026-11-10.json)。

## 实际工程绑定

- Python 3.11、FastAPI/Starlette、SQLite；SQLAlchemy 提供连接、表声明和记录构造。生产数据访问为具名直接 SQL，不使用 ORM 查询与持久化。
- `web/blueprints/` 保留旧模块路径，职责属于 Controller；公共切面位于 `controller/decorators/`。模型选择、提示词、会话和记忆编排归 Service；网络协议归 Client。
- 普通业务保留 `ok/error` JSON 契约，二进制/下载响应保持原格式；主服务 `/health` 固定为 `{"status":"ok"}`。
- 页面路径保留旧 URL，公开发布静态入口；数据迁移到 `/api/pages<页面路径>`，由 API 验证用户/开发者权限。浏览器渲染 HTML，服务器只返回静态资源和 JSON。
- 配置在 `config/` 合并与校验后形成启动快照。设备数据库配置、控制台明确保存的人脸/声纹配置等保留既有运行中更新流程；手工改文件需要重启。模型层不自行读取环境变量。
- 设备 WS 继续以 `device_id` 接入，适用原项目的受信设备网络部署。调试订阅另行验证有效会话或签名 token 及设备归属。没有为本项目虚构额外三方调用者类别。
- 正常停止先停调度触发、等待已运行提醒，再释放设备和客户端。调度单机按 SQLite 库文件锁唯一运行；跨机器部署必须显式只启用一个实例。
- 设备数据全清保留原有逐步删除、收集错误后可重试的业务契约；它跨数据库、文件和缓存，未冒充单个数据库事务。注册、会话保存、提醒更新、偏好批量修改及设备 ID 修改中的相关数据库写入按 Service 事务处理。

## 整改与证据

| 要求 ID / 范围 | 实现证据 | 行为验证及结论 |
| --- | --- | --- |
| H-MVC、H-CONTROLLER、H-SERVICE-ACTION | `controller/`、`service/`、`dao/`、`client/`、`schedule/`、`public/`、`model/`、`config/`；会话/记忆及 provider 选择已从 DAO/Client 分离 | 受影响的登录、会话、提醒、工具调用和页面回归通过 |
| H-CONTROLLER-REGISTRATION、H-ACCESS-DECLARATION | `controller/registry.py` 与每个路由的 `@access` | 缺访问声明、重复方法/路径在监听前失败；同路径不同方法允许；自定义 WS 别名也检查 |
| H-ASPECT、H-COMMON-DECORATORS、H-REQUEST-STATS | `controller/decorators/http.py` | 同步/异步异常、身份拒绝和 404 均记录最终状态、耗时、请求 ID；拒绝时不执行业务 |
| H-HEALTH、H-REQUEST-ID-HEADER | 唯一 `health_controller.py`；ASGI 公共切面 | `/health` 精确 JSON 契约；成功、失败均有 `X-Request-ID`，与日志一致 |
| H-AUTH-KIND、H-AUTH-BINDING | 会话、签名 token、用户有效性与开发者检查集中处理 | 匿名、伪造/失效凭证、已删除/停用用户拒绝；设备设置/文件/事件拒绝越权；调试 WS 同样验证身份和归属 |
| H-ERROR-OWNERSHIP、H-UNEXPECTED-ERROR、H-ERROR-CONVERSION | `model/errors.py` 与统一 HTTP 映射 | 底层原因保留 cause；未预期 5xx 固定消息，旧入口返回的异常明文也不再外泄 |
| H-MODEL-VALIDATION、H-MODEL-CONVERSION | `model/device_preferences.py`、`model/settings.py`、`model/llm.py` | 无效组合偏好在写库前失败；纯对象转换不重读环境；用户页面 DTO 不含密码哈希 |
| H-DAO-SQL、H-DAO-OPERATION-DECORATOR、H-DAO-NAMED-BINDING | `dao/decorators.py`；业务 DAO 的 select/select_one/insert/update/delete 类别声明 | SQL 具名绑定；控制参数 transaction 不进入 SQL；自增或显式字符串 ID 按插入方式返回 |
| H-DAO-SINGLE-RESULT、H-DAO-MAPPING-ERROR、H-DAO-ERROR-TYPE | 单条查询用 `one_or_none`；统一 `DataAccessError` | 零行为空，多行抛 cardinality；执行、约束冲突和映射失败不伪装成空结果 |
| H-SERVICE-TX-OWNER、H-SERVICE-TX-PASS、H-DAO-STANDALONE | `db/transaction.py`；DAO 显式接收 transaction | 同事务提交/回滚；下游失败和内层失败被捕获后仍不能提交；独立失败释放资源，后续操作可继续 |
| H-CONTROLLER-PATH、H-DAO-SQL-EXECUTION | `run_blocking` 适配同步 SQLite；独立 DAO 会话和请求级作用域 | 权限、偏好、会话和调度的同步访问在线程池执行；并发请求不共享 ORM Session |
| H-CLIENT-LAYOUT、H-CLIENT-BOUNDARY、H-CLIENT-CONFIG | `client/`；`service/*_routing.py`、`service/llm_service.py` | 设备配置与提示词先由 Service 解析，再交给协议客户端；兼容的 `runtime.resolve_*` 导入只做转发 |
| H-CLIENT-ERROR、H-CLIENT-MAPPING-ERROR、H-CLIENT-EMPTY | 共享 `ClientError`；ASR 协议错误和声纹专用错误继承它 | FunASR 保留 code/message/status；非法 JSON 与正常无结果有区别；保留原始 cause |
| H-CLIENT-RETRY、H-CLIENT-LIFECYCLE | 配置级客户端复用与应用关闭；豆包 TTS 单次尝试 | 连接失败只调用一次，抛统一错误；Service 不因网络失败自动换默认客户端重发；应用停止关闭连接池 |
| H-SCHEDULE-TRIGGER、H-SCHEDULE-IDENTITY、H-SCHEDULE-REGISTRATION | `schedule/reminders.py`、`schedule/executor.py` | 稳定任务 ID 和显式入口列表；重复任务、漏声明及无效间隔拒绝 |
| H-SCHEDULE-OVERLAP、H-SCHEDULE-RECORDS、H-SCHEDULE-FAILURE | 公共执行器与提醒任务跟踪 | 重入跳过并记录；成功/失败/跳过包含任务/触发标识、时间、状态、耗时和原因；默认无立即重试 |
| H-SCHEDULE-LIFECYCLE、H-SCHEDULE-SHUTDOWN、H-SCHEDULE-OWNER | FastAPI lifespan、scheduler.aclose、进程文件锁 | 依赖就绪且注册成功后启动；停止等待在途任务，不提前关闭它使用的资源 |
| H-CONFIG、H-CONFIG-PRECEDENCE、H-CONFIG-EFFECT | `config/`；启动 `configure_runtime` | 环境覆盖文件；无效端口阻止启动；普通文件修改不改变运行快照 |
| H-WEB、H-WEB-HOST、H-WEB-ADMIN、H-PUBLIC-*、H-WEB-FALLBACK | `public/site`、`admin`、`shared` 与已声明页面路由 | 匿名可加载资源；JSON API 继续检查权限；未知 API/资源/页面维持 404，不被 HTML 回退覆盖 |
| H-RUNTIME、H-LOCAL | 可复现检查命令、独立浏览器验收入口与根目录 CI | 临时服务真实监听、健康请求、浏览器注册和页面脚本验证通过；测试进程正常停止并清理 |

其中本次新增 SOP 专项为 `tests/test_sop_contract.py`；权限回归为 `tests/test_controller_auth.py`。共享数据库 fixture 使用独立库和独立运行数据副本，避免顺序依赖或改写真实设备数据。

## 本次实测

1. `ruff check src`、`python -m compileall -q src tests tools`、`git diff --check` 通过。
2. SOP 与相关行为验收：175 项通过，1 项既有豆包音色环境变量断言排除。覆盖权限、事务、会话、提醒、ASR、媒体、声纹、TTS、客户端异常及重试边界。
3. Chrome 实测注册成功，随后访问 18 个控制台页面，页面脚本异常为 0；随后完成正常停止、重启和持久化账号登录；覆盖首页、设备、记忆、提醒、人脸、设置、表达/剧情编辑、调试、烧录、外部服务和实验台。
4. 排除真实云端集成后的完整本地测试：922 通过、29 失败、3 错误。按失败测试 ID 与整改前临时副本比较，新增失败为 0，关闭了 20 个原基线失败/错误。旧问题包括已删除模块的测试导入、旧配置/界面断言、旧设备音频与调度测试假设，以及固件源码位置假设。

完整测试仍未全绿，因此没有用本次局部验收替代“整个仓库全部通过”的声明。逐条旧失败 ID 保存在 JSON 记录中。真实模型、真实云服务和真机联调本次未运行。

## 复验命令

在 `service/` 执行：

```bash
python tools/build_public.py
ruff check src
python -m compileall -q src tests tools
pytest -q tests/test_sop_contract.py tests/test_controller_auth.py tests/test_endpoint_auth.py \
  tests/test_auth.py tests/test_scheduled_task.py tests/test_session_context.py \
  tests/test_session_store.py tests/test_memory_store.py tests/test_asr_protocol.py \
  tests/test_asr_external.py tests/test_convo_audio.py tests/test_vpr_http_client.py \
  tests/test_tts_external.py tests/test_asr_doubao.py -k 'not doubao_config_speaker_precedence'
```

完整本地测试：

```bash
pytest tests -q --continue-on-collection-errors \
  --ignore=tests/test_ark_responses_integration.py \
  --ignore=tests/test_siliconflow_integration.py
```

浏览器验收使用测试专用应用、自选回环端口和临时数据库，不启动模型或连接硬件：

```bash
npm ci
CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" npm run test:browser
```

Linux/Windows 指定本机 Chrome/Chromium 的可执行文件路径。Node 22 与 Python 环境须已准备好。浏览器截图默认写入临时目录。Nunjucks 3.2.4 已附带版本、校验值及许可证，生产不从 CDN 加载它或 Vue。

## 兼容性与交接

- 升级健康检查调用方，使用 `status == "ok"`。
- 页面源文件从 `web/templates/` 迁到 `public/{site,admin}/views/`；修改后必须打包 `views.json`。控制台资源不再以服务端模板方式渲染。
- 旧 Python 内部导入按新的 Service/Client 归属调整；`db.sql_decorators` 保留兼容导出。不要在新代码中向 DAO 或 Client 重新放业务规则。
- 旧式 JSON 响应保持；5xx 的内部异常文本改为固定错误，定位时使用 `X-Request-ID` 查日志。
- 唯一调度实例之外设置 `DESKBOT_SCHEDULER_ENABLED=0`。正常关停等待任务完成，没有擅自增加超时取消或故障接替策略。
- 服务端 CI 已放到仓库根 `.github/workflows/service.yml`，覆盖静态资源构建、源码检查及整改相关契约测试；历史全库测试问题另行维护。

## 2026-11-15 外部 Agent 接入与本地管理退役

按 HTTP 服务工程 v0.4-draft 实现本次需求：在现有设备对话配置中加入相邻 flecket 数据库保存的 3 个 Agent，默认 `agent_265823d9ffd34dd7`（豆包llm_tts）；支持任意兼容 Realtime 服务地址和 `model`，服务默认地址可由 `FLECKET_URL` 覆盖，设备保存地址优先。Agent 清单只允许名称与模型标识，密钥仍由设备私有参数或环境变量提供，API 不回显。

移除独立服务管理网页、API 注册、运行时管理器装配、自动启动、守护与依赖本地状态的预热；取消没有实际探测依据的本地进程健康提示。外部 Agent 模式隐藏旧模型组合设置。空白人设与音色继承已保存的 Agent，保留工具定义；Client 负责协议与工具结果回传，Service 仅提供 `get_current_time` 执行器，未知工具返回明确错误。

验证结果：

- 10 个相关测试文件共 255 项通过，2 项已有首页测试排除（`CAMERA · LIVE` 文案与 `.home-deck-actions` 样式断言，与本次修改无关）。源码及新增 Agent 测试的 Ruff 检查通过，`git diff --check` 通过；旧 `test_endpoint_auth.py`、`test_robot_settings.py` 尚有原有导入顺序、重复 fixture 等 lint 问题，未在本次需求中整改。
- 隔离浏览器完成控制台页面遍历、正常停止与重启登录；专项验证预设切换、保存／刷新、密钥不回显、自定义云端 URL 归一化、旧管理路由 404 和 390px 手机布局。静态视图已重新打包。
- 使用真实 flecket 服务和现有凭证验证 Agent 握手，确认人设、`zh_female_vv_uranus_bigtts` 音色与查询时间工具保留；文字提问实际调用查询时间工具后收到 48kHz PCM 音频。没有上传麦克风录音，也没有向硬件播放本次测试音频。
- 当前使用 flecket 的设备 `brfk_68ee8f59d9245551` 已保存新 Agent 地址并清空人设／音色覆盖，原密钥保留；修改前数据库备份为 `data/opendesk.db.bak-20261115-agent-config`（仅本机读取权限）。其他设备不受此配置变更影响。

当前地址仍为本机 `127.0.0.1:9110`；未提供云端部署地址，未声称已迁移到公网云服务。未进行本次硬件录音、播放与口型实测。

## 云端上线验证

- 后台代码已推送至 GitHub `main`。发布验证使用从 Git 导出的干净副本：255 项通过，仍排除前述 2 项已有首页断言。音色列表测试已改用临时样本，移除对本机已安装模型的依赖。
- 用户明确授权后，在云端 flecket 中创建所选 Agent 及后台专用密钥，复用已有云端模型。连接密钥只保存在服务器，不写入代码仓库。
- 先验证旧数据库副本升级和候选后台启动，再切换正式服务。后台已配置开机启动和失败重启；上线后服务正常运行。
- 公网浏览器确认登录页和健康接口正常，已退役的服务管理入口返回 404。真实 Agent 已完成查询时间工具调用并返回语音；正式数据库完整性检查通过，已有账号、设备和定时任务记录数量保持一致。
- 发布前已保存代码、数据库和配置备份。服务器地址、备份位置、Agent 标识、设备标识和凭证等运维信息仅保留在私有交付记录中。本次没有刷写固件，也未进行实体设备麦克风／扬声器测试。

## 交互优化第一轮

- 控制台主界面保留顶栏和侧栏，内容准备就绪后再替换；页面上下文与模板并行读取，模板按构建版本缓存。慢请求显示轻量提示，失败和超时保留当前内容并提供重试；导航竞争只接受最后一次结果，浏览器后退可用。Vue 页面离开时执行卸载，关闭其资源。
- 设备选择按标签页保持，只保存非敏感设备标识。页面 API 明确携带设备上下文，后端在业务执行前检查归属，防止会话切换造成错写。切换设备重新装载表单；未保存编辑和进行中的写操作受离开保护。
- 共用请求层处理网络中断、超时、权限和登录过期，业务表单清除旧提示、保留失败输入，并锁定重复点击或回车提交。列表区分读取中、无设备、空列表与失败状态。
- 记忆新增使用请求标识，经 Service 利用已有设备／标题唯一约束实现原子防重，不增加表或修改既有数据；并发请求和已写入但响应丢失后的重试返回同一记录，标识被用于不同内容时拒绝。
- 新增 `test_ux_requests.py` 和隔离浏览器交互脚本，覆盖设备错位、归属拒绝、并发防重、解绑、多标签页选择、草稿离开保护、页面壳保留、断网重试、导航失败及请求竞争。浏览器测试仅使用临时账号和数据库，不接入硬件。
