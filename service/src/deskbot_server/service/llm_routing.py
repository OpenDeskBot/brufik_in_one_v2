"""设备及系统大模型的选择与配置；不执行网络请求。"""

from __future__ import annotations

import logging
from dataclasses import replace

from deskbot_server.client.llm.runtime import (
    ARK_DEVICE_PROTOCOL,
    ARK_OPENAI_BASE_URL,
    DEVICE_LLM_PROVIDERS,
    LOCAL_LLM_PROVIDERS,
    SILICONFLOW_BASE_URL,
    SILICONFLOW_DEFAULT_MODEL,
    ResolvedLlmConfig,
    _normalized_protocol,
    _resolve_api_base,
    build_chat_model,
    is_siliconflow_base,
    read_api_key_env,
    resolve_siliconflow_api_key,
    siliconflow_extra_body,
)
from deskbot_server.config import get_config as load_config

logger = logging.getLogger("deskbot-server")


def resolve_system_llm_config(cfg: dict | None = None) -> ResolvedLlmConfig:
    """系统默认 LLM：只读 config.yaml ``llm`` 段。

    ``llm_provider`` 为空 / 非法的设备（含新绑定设备）都走这里，因此本函数同时决定了
    「系统默认」与「新设备默认」两件事。

    密钥来源：
    - 本地免费引擎：恒为空，不需要密钥；
    - 云端（当前默认指向硅基流动）：由 ``llm.api_key_env`` **指名**服务端环境变量
      （如 ``SILICONFLOW_API_KEY``），本函数只读该变量名，绝不接受内联明文
      （``config.yaml`` 进 git）。未配置 ``api_key_env`` 时 api_key 仍为空，行为同旧版。

    ``llm.extra_body`` 可显式指定模型专属请求体字段；未指定时按 base_url host 命中预设表。
    """
    if cfg is None:
        cfg = load_config()
    llm_cfg = dict(cfg.get("llm") or {})
    protocol = _normalized_protocol(llm_cfg.get("protocol") or "openai")
    model_name = str(llm_cfg.get("model_name") or "").strip()
    base_url = str(llm_cfg.get("base_url") or "").strip()
    resolved_base = _resolve_api_base(protocol, base_url)
    api_key = read_api_key_env(str(llm_cfg.get("api_key_env") or ""))
    raw_extra = llm_cfg.get("extra_body")
    if isinstance(raw_extra, dict):
        extra_body = dict(raw_extra) or None
    else:
        extra_body = siliconflow_extra_body(model_name) if is_siliconflow_base(resolved_base) else None
    try:
        ctx = int(llm_cfg.get("context_window"))
    except (TypeError, ValueError):
        ctx = 0
    return ResolvedLlmConfig(
        model=build_chat_model(protocol, model_name),
        api_key=api_key,
        api_base=resolved_base,
        protocol=protocol,
        source="system",
        display_name=f"系统默认 ({model_name})",
        context_window=ctx if ctx > 0 else None,
        extra_body=extra_body,
    )


def resolve_device_llm_provider(device_id: str | None) -> str | None:
    """设备级 LLM provider（devices.llm_provider，白名单校验）。

    空 / 非法值 / 无设备 → None（回落系统默认，见 resolve_system_llm_config）。
    """
    did = str(device_id or "").strip()
    if not did:
        return None
    from deskbot_server.dao.device_mapper import get_llm_provider

    provider = str(get_llm_provider(did) or "").strip()
    if provider not in DEVICE_LLM_PROVIDERS:
        if provider:
            logger.warning("[LLM] 设备 llm_provider 非法/未知: %r（回落系统默认）", provider)
        return None
    return provider


def _local_device_llm_config(provider: str) -> ResolvedLlmConfig:
    """本地固定端点（minicpm / qwen / unee）：免 API Key，无参数可配。"""
    base_url, model_name = LOCAL_LLM_PROVIDERS[provider]
    return ResolvedLlmConfig(
        model=model_name,
        api_key="",
        api_base=base_url,
        protocol="openai",
        source="device",
        display_name=f"本地 {provider}（{model_name}）",
        context_window=8192 if provider == "realtime" else None,
    )


def _ark_device_llm_config(device_id: str) -> ResolvedLlmConfig:
    """按设备 llm_param["ark"] 构造云端 ark 配置（密钥/模型仅设备级）。

    model_name 缺失抛 ValueError（绝不回落 config.yaml，避免本地模型 ID 串位到云端）；
    base_url 空 → 内置默认；api_key 空则留空，真正调用时由 _validate_api_key 报错。
    """
    from deskbot_server.dao.device_mapper import get_llm_param

    param = get_llm_param(device_id)
    ark = param.get("ark")
    ark = ark if isinstance(ark, dict) else {}
    model_name = str(ark.get("model_name") or "").strip()
    if not model_name:
        raise ValueError(
            "该设备 ark LLM 配置缺少模型 ID（火山方舟推理接入点 ep-…）："
            "请在机器人设置 → LLM → ark「配置」中为该设备填写"
        )
    base_url = str(ark.get("base_url") or "").strip().rstrip("/") or ARK_OPENAI_BASE_URL
    return ResolvedLlmConfig(
        model=build_chat_model(ARK_DEVICE_PROTOCOL, model_name),
        api_key=str(ark.get("api_key") or "").strip(),
        api_base=base_url,
        protocol=ARK_DEVICE_PROTOCOL,
        source="device",
        display_name=f"ark · {model_name}",
    )


def build_siliconflow_config(
    model_name: str | None, *, source: str = "system", display_name: str | None = None
) -> ResolvedLlmConfig:
    """硅基流动（OpenAI 兼容）配置：**密钥只来自服务端 .env**，与设备无关。

    系统默认、设备级 siliconflow、试聊三条路共用，保证密钥与 extra_body 口径一致。
    """
    model = str(model_name or "").strip() or SILICONFLOW_DEFAULT_MODEL
    return ResolvedLlmConfig(
        model=build_chat_model("openai", model),
        api_key=resolve_siliconflow_api_key(),
        api_base=SILICONFLOW_BASE_URL,
        protocol="openai",
        source=source,
        display_name=display_name or f"SiliconFlow · {model}",
        extra_body=siliconflow_extra_body(model),
    )


def _siliconflow_device_llm_config(device_id: str) -> ResolvedLlmConfig:
    """设备级 siliconflow：只从 llm_param["siliconflow"] 取 model_name。

    与 ark 相反，**模型缺失不抛错**而是回落预设默认：``PUT /api/devices/{id}/llm``
    （app_bp）不校验 provider 白名单，客户端可以只写 provider 不带模型；抛错会让整页
    ``_llm_status`` 退化成错误展示。密钥恒由服务端 .env 提供。
    """
    from deskbot_server.dao.device_mapper import get_llm_param

    param = get_llm_param(device_id)
    raw = param.get("siliconflow")
    sf = raw if isinstance(raw, dict) else {}
    return build_siliconflow_config(str(sf.get("model_name") or "").strip(), source="device")


def resolve_device_decision_provider(device_id: str | None) -> str:
    """设备级「动作/表情决策模型」provider。

    默认 ``unee``：设备不存在、列为空或值非法都回落 unee（用户拍板的默认值）。
    ark / siliconflow 复用该设备的 ``llm_param``（与大脑 LLM 共用同一份云端配置）。
    """
    from deskbot_server.dao.device_mapper import get_decision_llm_provider

    did = str(device_id or "").strip()
    provider = str(get_decision_llm_provider(did) or "").strip() if did else ""
    if provider in DEVICE_LLM_PROVIDERS:
        return provider
    return "unee"


def _resolve_provider_llm_config(
    provider: str | None, device_id: str | None, cfg: dict | None = None
) -> ResolvedLlmConfig:
    """按显式 provider 解析配置（大脑 LLM 与决策模型共用）。

    provider 为空/未知 → 系统默认（config.yaml llm 段）。
    """
    if provider in LOCAL_LLM_PROVIDERS:
        return _local_device_llm_config(provider)
    if provider == "ark":
        return _ark_device_llm_config(str(device_id or "").strip())
    if provider == "siliconflow":
        return _siliconflow_device_llm_config(str(device_id or "").strip())
    return resolve_system_llm_config(cfg)


def resolve_llm_config(device_id: str | None = None, cfg: dict | None = None) -> ResolvedLlmConfig:
    """解析设备生效 LLM 配置（设备级真源：devices.llm_provider / llm_param）。

    优先级：minicpm / qwen / unee（本地固定端点）→ ark（llm_param["ark"]，含密钥/模型）→
    siliconflow（llm_param["siliconflow"] 只存模型，密钥走服务端 .env）→
    系统默认（config.yaml llm 段）。顶层键 context_window / native_tools 自
    devices.llm_param 合并（未填 → None，调用方回退默认预算）。
    ``cfg`` 仅供测试注入自定义 config 文件内容。
    """
    provider = resolve_device_llm_provider(device_id)
    resolved = _resolve_provider_llm_config(provider, device_id, cfg)
    did = str(device_id or "").strip()
    if not did:
        return resolved
    from deskbot_server.dao.device_mapper import get_llm_param

    param = get_llm_param(did)
    raw_cw = param.get("context_window")
    try:
        cw = int(raw_cw)
    except (TypeError, ValueError):
        return resolved
    if cw > 0:
        return replace(resolved, context_window=min(cw, 8192) if provider == "realtime" else cw)
    return resolved


def resolve_decision_llm_config(device_id: str | None = None) -> ResolvedLlmConfig:
    """解析设备「动作/表情决策模型」配置（devices.decision_llm_provider，默认 unee）。

    决策 prompt 很小，不继承 ``context_window``；云端 provider 复用该设备的
    ``llm_param``（ark 需已配模型/密钥，siliconflow 密钥走服务端 .env）。
    """
    provider = resolve_device_decision_provider(device_id)
    return _resolve_provider_llm_config(provider, device_id)
