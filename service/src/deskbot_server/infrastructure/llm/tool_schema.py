"""OpenAI 原生 tools（function calling）schema 定义。

参数键与 ``llm_tool_runner.execute_llm_tools`` / 各 service 工具读取的**平铺键一一对应**：
原生调用解析后 ``raw = {"tool": name, **arguments}`` 可直接进现有执行器，执行层零改动。
description 用中文承载触发规则（行为约束）；本地小模型对嵌套 oneOf 遵循率差，
``schedule_task`` 采用平铺 action+enum+条件字段全 optional 的宽松建模。
"""

from __future__ import annotations

from typing import Any


def _fn(name: str, description: str, required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# ───────────────────── 第一批：纯函数工具 ─────────────────────

def _batch1_schemas() -> list[dict[str, Any]]:
    return [
        _fn(
            "memory_add",
            "把值得长期记住的用户信息存入长期记忆（名字/喜好/事实/约定等）。"
            "text 写用户原意的完整短句；随口闲聊不要存。",
            ["text"],
            {"text": {"type": "string", "description": "要记住的内容，完整短句"}},
        ),
        _fn(
            "memory_delete",
            "删除一条长期记忆。id 必须取自 system 提示中长期记忆清单方括号中的 id，禁止编造。",
            ["id"],
            {"id": {"type": "string", "description": "记忆 id（见 system 长期记忆方括号）"}},
        ),
        _fn(
            "schedule_task",
            "定时/提醒任务增删改查（北京时间东八区）。**用户要求定时/提醒时必须调用，禁止仅口头答应。**"
            "action=create 时用 task + task_kind(once/recurring) + cron 或 delay_minutes；"
            "cron 为「分 时 日 月 周」五段，如每天8点 \"0 8 * * *\"、明天9点 \"0 9 <明日日期> <明日月份> *\"；"
            "其余 action 用 id（update 可带 task/cron/enabled）。创建无需 session_id。",
            ["action"],
            {
                "action": {"type": "string", "enum": ["create", "list", "get", "update", "delete"], "description": "操作类型"},
                "task": {"type": "string", "description": "提醒内容（create/update）"},
                "task_kind": {"type": "string", "enum": ["once", "recurring"], "description": "once 一次性 / recurring 周期性（create 必填）"},
                "cron": {"type": "string", "description": "五段 cron（分 时 日 月 周），与 delay_minutes 二选一"},
                "delay_minutes": {"type": "integer", "description": "相对延迟分钟数（如「两分钟」→ 2），与 cron 二选一"},
                "id": {"type": "string", "description": "任务 id（get/update/delete 用）"},
                "enabled": {"type": "boolean", "description": "update 时是否启用"},
            },
        ),
        _fn(
            "webfetch",
            "抓取指定网页并返回正文文本（结果可能被截断）。仅当需要读取某个具体网址内容时使用。",
            ["url"],
            {"url": {"type": "string", "description": "http/https 网址"}},
        ),
        _fn(
            "websearch",
            "网络搜索获取摘要。仅当问题需要实时/外部信息（新闻、天气、股价、赛事等）时调用；"
            "query 用简洁中文关键词，如「今天北京天气」。",
            ["query"],
            {
                "query": {"type": "string", "description": "搜索关键词（中文）"},
                "max_results": {"type": "integer", "description": "返回条数，默认 5，上限 10"},
            },
        ),
        _fn(
            "session",
            "查询当前与最近对话 session（10 分钟无对话自动开新 session）。",
            ["action"],
            {
                "action": {"type": "string", "enum": ["current", "list", "get"], "description": "current 当前 / list 列表 / get 详情"},
                "limit": {"type": "integer", "description": "list 条数，默认 10"},
                "session_id": {"type": "string", "description": "get 指定 session；省略读当前"},
            },
        ),
    ]


# ───────────────────── 第二批（A2 阶段启用）─────────────────────

def _batch2_schemas(*, device_id: str | None = None, quest_task_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """register_face / register_voiceprint + 剧情任务工具（任务 id 动态注入 description）。"""
    out: list[dict[str, Any]] = [
        _fn(
            "register_face",
            "把当前画面中的人脸注册/更新到档案。省略 face_id = 当前画面唯一人脸；"
            "多人画面必须从每轮 user 消息「图像识别」行取 faceid= 指定，或先向用户澄清。",
            ["name"],
            {
                "name": {"type": "string", "description": "姓名"},
                "face_id": {"type": "integer", "description": "画面人脸编号（见 user 消息图像识别）"},
            },
        ),
        _fn(
            "register_voiceprint",
            "记住刚说话的人的声音（注册样本来自最近一次对话语音）。用户说「记住我的声音/我叫xx」时必须调用；"
            "若返回样本不足的提示，请引导用户先对机器人说一句完整的话再重新注册。",
            ["name"],
            {"name": {"type": "string", "description": "姓名"}},
        ),
    ]
    if quest_task_ids is not None and quest_task_ids:
        ids_text = ", ".join(quest_task_ids)
        out.append(
            _fn(
                "update_task_result",
                f"判断进行中剧情任务的成败并落结果（当前可用任务 id：{ids_text}）。"
                "success_condition 满足则置 success、failure_condition 满足则置 failed；"
                "置终态后会自动向后继任务传播。result 必填，写用户原意。",
                ["task_id", "status", "result"],
                {
                    "task_id": {"type": "string", "description": "目标任务 id"},
                    "status": {"type": "string", "enum": ["success", "failed"], "description": "判定结果"},
                    "result": {"type": "string", "description": "成功结果/失败原因（口语转述）"},
                },
            )
        )
        out.append(
            _fn(
                "update_task_strategy",
                f"根据用户反馈更新某任务的处理策略（当前可用任务 id：{ids_text}）。",
                ["task_id", "strategy"],
                {
                    "task_id": {"type": "string", "description": "目标任务 id"},
                    "strategy": {"type": "string", "description": "新的处理策略"},
                },
            )
        )
    return out


# ───────────────────── 第三批：用户社交（按人归档）工具 ─────────────────────

def _user_social_schemas() -> list[dict[str, Any]]:
    """update_user_info / update_daily_task：识别到具体用户时按人归档与记账。

    独立于 batch1（不破坏 NATIVE_TOOL_NAMES_BATCH1 的既有精确断言），
    随 batch2 开关默认启用。
    """
    return [
        _fn(
            "update_user_info",
            "用户当面告知姓名/性别/年龄/家庭/住址/爱好/职业等个人信息时调用，"
            "按人归档到该用户资料文件（该用户在场被识别到时才会被参考）。"
            "只写用户刚新披露的事实短句；纠正旧信息写成「更正:…」新行；"
            "不要整段重述已写过的内容。参数键**必须**为 user_name 与 chat_message："
            "user_name 写被识别到的人名（中文/字母/数字），chat_message 写原意的"
            "完整短句（如「我住在北京市海淀区」），不需要带时间；不要拆成 user/"
            "location/age 等散键。",
            ["user_name", "chat_message"],
            {
                "user_name": {"type": "string", "description": "用户姓名（须为中文/字母/数字）"},
                "chat_message": {"type": "string", "description": "用户透露的信息，原意短句"},
            },
        ),
        _fn(
            "update_daily_task",
            "主动任务记账：早上/中午/晚上第一次见到认识的人时主动问候、饭点询问吃饭、"
            "或距上次对话较久表达思念——开口**前**必须先调用本工具记账一次，再开口说话；"
            "用户当面交代的饮食/日程等完成事项也可记。参数键**必须**为 user_name 与 "
            "message：user_name 写被问候/关心的用户姓名，message 用第一人称一句话写这次"
            "主动互动的内容（如「我跟小明说了早上好」「我问了小明中午吃了什么」），"
            "不用自带时间，服务端自动补；同一意图已记录则不重复（同时段问候只记一次、"
            "思念 30 分钟内不重复）。",
            ["user_name", "message"],
            {
                "user_name": {"type": "string", "description": "被问候/关心的用户姓名"},
                "message": {"type": "string", "description": "本次主动互动内容，第一人称一句话"},
            },
        ),
    ]


def build_native_tool_schemas(
    *, device_id: str | None = None, include_batch2: bool = True
) -> list[dict[str, Any]]:
    """输出当前启用的原生工具 schema（供每轮 tools 参数）。

    batch1 = 纯函数六工具；batch2 = 人脸/声纹注册 + 剧情任务（无 running 任务时
    quest 工具不产出；任务 id 动态注入 description，不进 parameters enum）；
    batch3（随 batch2 开关）= 用户社交按人归档两工具，恒在。
    """
    schemas = _batch1_schemas()
    if include_batch2:
        task_ids: list[str] | None = None
        if device_id:
            from deskbot_server.service.quest_service import QuestService

            calls = QuestService().get_tool_calls(str(device_id))
            if calls:
                task_ids = list(calls[0].get("available_task_ids") or [])
        schemas += _batch2_schemas(device_id=device_id, quest_task_ids=task_ids)
        schemas += _user_social_schemas()
    return schemas


NATIVE_TOOL_NAMES_BATCH1 = [s["function"]["name"] for s in _batch1_schemas()]
