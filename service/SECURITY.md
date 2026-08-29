# Security

请勿在公开 Issue 报告漏洞；请私信维护者并说明影响与复现步骤。

## 密钥与敏感文件

- 勿提交 `.env`、`data/.free_api_key`、`data/.builtin_password`
- `data/opendesk.db` 含用户账号与设备绑定信息，勿提交生产库
- `data/{device_id}/`（及历史 `data/device/`、`data/{device_id}_{pin}/`）含设备 session、记忆等人数据，已加入 `.gitignore`，勿提交
- 勿将 `hardware/firmware/deskbot_config.h` 中的真实 WiFi 密码 / 内网 IP 提交到公开仓库

## 网络暴露

- 默认绑定 `0.0.0.0`（`:9000` 设备链路、`:5050` 控制台）：公网暴露前请加防火墙、反向代理与 TLS
- LLM 凭据（`ARK_API_KEY` 等）仅通过环境变量 / `.env` 注入，勿写入 `config.yaml` 或日志

## 认证与隔离

- Web 控制台（`:5050`）：邮箱 + 密码注册登录；无默认账号、无邮件找回密码
- 生产请设置随机长字符串 `DESKBOT_WEB_SECRET_KEY`
- **设备**连接 `:9000/asr_chat`：仅须 `device_id`（无 API Key / PIN）
- **Web / HTTP / 调试订阅**（`/api/*`、`/camera_view`、`/device_pipeline` 订阅侧）：须控制台登录会话或 `debug_token`（`/api/debug/ws_token` 签发）
- 本地联调脚本可读取 `data/.free_api_key`（若存在），不用于任何服务端鉴权
- 设备操作、定时任务、记忆、人脸数据按账号绑定的 `device_id` 隔离（`data/{device_id}/`）

## 自托管建议

- 定期轮换凭据（`DESKBOT_WEB_SECRET_KEY`、LLM Key）；限制 `:5050` / `:9000` 仅内网可达
- 备份 `data/opendesk.db` 与 `data/{device_id}/`（若需保留用户数据）
