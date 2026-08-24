from __future__ import annotations

import hashlib
import json
from typing import Final


# Descriptions state capability and evidence only. They intentionally do not
# encode a workflow, prerequisite order, intent taxonomy, or next action.
_TOOL_DEFINITIONS: Final[dict[str, dict[str, object]]] = {
    "recognize_image": {
        "description": "识别当前会话中的买家图片，返回可核验的票务页面事实；无法识别时返回缺失项和稳定错误码。",
        "properties": {},
    },
    "resolve_ticket_identity": {
        "description": "根据当前会话中的文字、候选项和已保存事实解析影院、影片、日期及场次身份，返回唯一结果或仍缺少的事实。",
        "properties": {},
    },
    "resolve_showtime": {
        "description": "用已识别的票务事实匹配官方场次，返回唯一场次事实或无法唯一匹配的原因。",
        "properties": {},
    },
    "quote_realtime": {
        "description": "读取当前唯一场次的实时座位和价格并形成权威报价；返回金额、张数、有效期或缺失事实。",
        "properties": {},
    },
    "read_active_quote": {
        "description": "读取当前会话已发送且仍有效的最新报价事实。",
        "properties": {},
    },
    "show_available_wplus_seats": {
        "description": "查询当前场次实时可选的W+座位；可按买家已表达的排数筛选，返回实时座位事实。",
        "properties": {},
    },
    "record_seat_preference": {
        "description": "保存买家在当前会话表达的座位偏好或按原图圈选位置出票的指令，返回是否已安全记录。",
        "properties": {},
    },
    "confirm_active_quote": {
        "description": "尝试确认当前有效报价；工具从权威会话读取报价并验证确认条件，返回确认事实或拒绝原因。",
        "properties": {},
    },
    "read_linked_order": {
        "description": "读取当前会话关联订单的最新平台状态，不接受模型提供订单号。",
        "properties": {},
    },
    "change_order_price": {
        "description": "尝试把当前会话关联的未付款订单改为已确认报价金额；工具自行注入订单和金额并执行全部权限、租户、状态、幂等与金额门禁。",
        "properties": {},
    },
    "create_manual_task": {
        "description": "为当前会话创建人工处理事项，返回任务是否创建或已存在；不代表业务事项已经完成。",
        "properties": {
            "summary": {"type": "string", "description": "供人工理解问题的简短摘要", "maxLength": 300},
        },
    },
    "get_manual_task_status": {
        "description": "读取当前会话最近人工任务的最新状态。",
        "properties": {},
    },
}

_TOOL_RUNTIME_CONTRACTS: Final[dict[str, dict[str, object]]] = {
    "recognize_image": {"effect": "read", "requires": [], "reads": ["current_message_images"], "writes": []},
    "resolve_ticket_identity": {"effect": "read", "requires": [], "reads": ["conversation_identity"], "writes": []},
    "resolve_showtime": {"effect": "read", "requires": ["recognized_identity"], "reads": ["showtime_catalog"], "writes": []},
    "quote_realtime": {"effect": "external_temporary_write", "requires": ["unique_showtime"], "reads": ["wanda_seats"], "writes": ["temporary_quote_probe"]},
    "read_active_quote": {"effect": "read", "requires": [], "reads": ["active_quote"], "writes": []},
    "show_available_wplus_seats": {"effect": "read", "requires": ["unique_showtime"], "reads": ["wanda_seats"], "writes": []},
    "record_seat_preference": {"effect": "write", "requires": [], "reads": [], "writes": ["conversation_preference"]},
    "confirm_active_quote": {"effect": "write", "requires": ["explicit_current_confirmation"], "reads": ["active_quote"], "writes": ["quote_confirmation"]},
    "read_linked_order": {"effect": "read", "requires": [], "reads": ["linked_order"], "writes": []},
    "change_order_price": {"effect": "write", "requires": ["confirmed_quote", "linked_unpaid_order"], "reads": ["active_quote", "linked_order"], "writes": ["order_price"]},
    "create_manual_task": {"effect": "write", "requires": [], "reads": [], "writes": ["manual_task"]},
    "get_manual_task_status": {"effect": "read", "requires": [], "reads": ["manual_task"], "writes": []},
}

AGENT_TOOL_VERSION: Final = "native-tools-" + hashlib.sha256(
    json.dumps(_TOOL_DEFINITIONS, ensure_ascii=False, sort_keys=True).encode("utf-8")
).hexdigest()[:12]


def available_tool_names() -> frozenset[str]:
    return frozenset(_TOOL_DEFINITIONS)


def native_tools(names: list[str]) -> list[dict[str, object]]:
    unknown = set(names) - available_tool_names()
    if unknown:
        raise ValueError(f"unknown agent tools: {', '.join(sorted(unknown))}")
    tools: list[dict[str, object]] = []
    for name in names:
        definition = _TOOL_DEFINITIONS[name]
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": definition["description"],
                "parameters": {
                    "type": "object",
                    "properties": definition["properties"],
                    "additionalProperties": False,
                },
            },
        })
    return tools
