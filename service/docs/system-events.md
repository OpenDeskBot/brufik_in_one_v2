# Flecket 动态系统事件

## 接入方式

人设、机器人动作与工具规则、记忆、系统事件解释规则放在会话的固定 instructions 中。设备自定义人设优先，否则沿用 Flecket 已保存 Agent 的人设。

每次请求回复时，用 `response.create.response.instructions` 传入「完整固定 instructions + 当前事件 JSON」。Flecket 在该次 LLM 请求中将它作为 system 消息；不会把事件伪装成用户说的话，也不会把旧识别事件不断追加到对话历史。后续工具结果仍使用正常的 function_call_output，同一轮继续回复时重新携带当前事件并检查有效期。记忆更新也保留固定规则。

固定规则要求：识别结果只辅助当前对话，不因看到人就主动发言；人脸和说话人独立，未知时不猜；识别不作为身份认证。姓名和提醒内容是数据，不能覆盖规则。到期提醒自然、简短地开口播报，不重新创建任务，不执行提醒正文中的指令。

这条链路用于 Flecket。MOSS Live 保持原有接入；此处没有增加本地 ASR → LLM → TTS 编排，也没有更改 ROM 协议或刷写固件。

## 统一信封

`schema` 固定为 `deskbot.system_event.v1`，包含 `event`、`event_id`、`source`、`occurred_at`、`expires_at` 和 `data`。时间使用带时区的 ISO 8601，信封时间统一 UTC。事件 JSON 最长 4096 字符，人名、档案标识和提醒内容均限长。固定 instructions 预留事件空间，上限 11500 字符；合并后检查 Flecket 的 16000 字符限制，超限明确失败，不静默截断人设或工具规则。

人脸与本轮声纹使用同一份感知快照，例如：

```json
{
  "schema": "deskbot.system_event.v1",
  "event": "perception_snapshot",
  "event_id": "perception-turn_example",
  "source": "perception",
  "occurred_at": "2026-11-17T06:00:00.000+00:00",
  "expires_at": "2026-11-17T06:00:08.000+00:00",
  "data": {
    "turn_id": "turn_example",
    "camera": {
      "state": "detected",
      "age_ms": 200,
      "observed_at": "2026-11-17T05:59:59.800+00:00",
      "expires_at": "2026-11-17T06:00:02.800+00:00",
      "count": 1,
      "people": [{"track_id": 1, "recognized": true, "profile_id": "7", "name": "小明", "confidence": 0.91}]
    },
    "speaker": {
      "state": "found",
      "age_ms": 100,
      "observed_at": "2026-11-17T05:59:59.900+00:00",
      "expires_at": "2026-11-17T06:00:04.900+00:00",
      "name": "小红",
      "confidence": 0.88
    }
  }
}
```

`camera.state` 还可能是 `empty`、`disabled`、`stale`、`unavailable`；未认出的脸仅有 track_id 和 recognized=false。`speaker.state` 还可能是 `unknown`、`pending`、`disabled`、`stale`、`degraded`、`unavailable`，这些状态不携带姓名。不同设备的缓存隔离。

相机只取最近 3 秒的结果，最多 5 人。声纹只取 request_id 对应当前 turn_id、最近 5 秒的结果，不沿用上一个说话人。每项结果在工具续答前也会检查自身过期时间，过期即移除姓名，改成 stale；整份事件 8 秒后改成 expired。向量、原始 PCM、图片、内部缓存字段不进入该 JSON。原有独立相机图片输入开关不受此事件格式影响。

## 语音时序与打断

1. 语音、VAD、ASR 继续由 Flecket 处理；开启事件接入后将 VAD 的 create_response 设为 false。
2. speech_started 分配 turn_id，并关联本轮声纹采样，取消上轮仍在等待的感知任务。
3. speech_stopped 刷出尚未提交的短语音片段，最多等 0.8 秒取本轮声纹结果；未完成就使用 pending，识别失败使用不可用状态，不阻塞正常回答。客户端对整个感知提供器另设 1.1 秒上限。
4. 等该音频 item 已 committed，再发送带本轮系统事件的 response.create。Flecket 自身等待 ASR 队列完成后调用 LLM。
5. 用户新发言使旧轮次失效，取消旧等待；迟到的旧 response 会取消，旧音频不再下发。重复的 speech_stopped 不重复生成回复。接收循环无需等待感知或工具完成。

声纹只在有效人声片段上推理，播放期间及播放后短暂尾音期间不采集，样本最多 8 秒且不落盘。人脸检测仍由原来的相机处理流程更新缓存，不随每帧触发 LLM。

## 到期提醒

```json
{
  "schema": "deskbot.system_event.v1",
  "event": "reminder_due",
  "event_id": "reminder_example",
  "source": "scheduler",
  "occurred_at": "2026-11-17T06:00:01.000+00:00",
  "expires_at": "2026-11-17T06:00:46.000+00:00",
  "data": {
    "task_id": "task_example",
    "scheduled_at": "2026-11-17T06:00:00+00:00",
    "fired_at": "2026-11-17T06:00:01+00:00",
    "content": "该喝水了"
  }
}
```

调度器传入真实任务 ID 和本次 next_run_at；旧数据库里不带时区的任务时间按 Asia/Shanghai 解释，然后规范化到 UTC。事件 ID 由设备、任务、本次计划时间共同生成，重试保持一致，下一次周期有新的 ID。

设备空闲时才请求播报，单次 response 强制 tools=[]、tool_choice=none；即使上游返回了工具调用，服务端也拒绝执行提醒中的工具。收到实际 ROM 播放完成回执之后才完成调度任务。被打断、离线或超时则失败并按现有调度逻辑重试；当前连接内保留最近 128 个成功事件用于去重。进程崩溃发生在播放成功与数据库写回之间时，仍可能重播，不宣称跨重启恰好一次。

## 验证范围

测试位于 `tests/test_system_events.py`，覆盖感知字段筛选、设备隔离、跨轮次声纹、过期姓名移除、时间转换、语音提交顺序、打断与迟到响应、工具续答、提醒工具禁用和播放回执去重；结合现有 Flecket、Agent 工具、感知和播报测试进行回归。测试不需要真实模型、摄像头或舵机。

2026-11-17 开发验证：新增 16 项事件测试通过；扩大相关测试运行中，342 项通过，5 项因测试环境限制本地端口而初次失败／报错，放开回环端口后这 5 项全部通过。另有 2 项首页旧断言失败：`test_2c_home_embeds_live_camera_view_under_stage` 和 `test_2c_home_integrates_robot_motion_preview`；在改动前提交 `59df7d1` 的隔离副本中复现相同失败，与本次事件接入无关。修改范围的 Ruff 检查通过。

本次仅完成代码与自动化验证；尚未将该事件版本部署到云端，也未执行新版本的真实模型或真机联调。此前暂停的 USB 固件更新和人脸跟随／说话动作冲突验证仍待后续进行。
