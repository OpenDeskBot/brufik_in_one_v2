# 文档目录

本仓库文档与当前实现对齐，入口为根目录 [README.md](../README.md)。固件工具链见 [../../hardware/README.md](../../hardware/README.md)（Arduino-ESP32 3.3.9 / IDF 5.5.4 / pioarduino）。

## 协议与设备

| 文档 | 内容 |
|------|------|
| [api_interfaces.md](./api_interfaces.md) | Web 控制台与 deskbot 设备服务接口清单 |
| [esp32_pb_protocol.md](./esp32_pb_protocol.md) | HTTP 启动发现 schema 2、独立媒体通道、PB v2／回执及当前限制 |

## 服务

| 文档 | 内容 |
|------|------|
| [SERVER.md](./SERVER.md) | 主服务：端口、控制台、LLM 工具、豆包 TTS 配置 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 代码分层、对话与定时任务链路 |
| [SERVER.md#文件记忆](./SERVER.md#文件记忆) | 每日记录、人物资料、记忆工具、迁移与遗忘 |
| [external_services.md](./external_services.md) | 外部进程服务管理（manifest 契约 / 状态机 / 管理后台） |
| [external_service_design.md](./external_service_design.md) | 外部服务设计模式与后台交互规范（insightface-engine 为例，含新增服务清单与踩坑记录） |

## 联调工具

| 文档 | 内容 |
|------|------|
| [../tools/README.md](../tools/README.md) | WAV / 麦克风 / 相机测试客户端 |

## 协作与安全

| 文档 | 内容 |
|------|------|
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | 贡献流程与测试 |
| [../SECURITY.md](../SECURITY.md) | 密钥、鉴权与部署安全 |
