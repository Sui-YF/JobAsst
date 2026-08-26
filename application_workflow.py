from __future__ import annotations

from dataclasses import dataclass

import database


STATES = {
    "NEW", "RESUME_READY", "JD_READY", "ANALYZING", "NEEDS_CLARIFICATION",
    "READY_FOR_STRATEGY", "STRATEGY_READY", "AWAITING_STRATEGY_APPROVAL",
    "EDITING", "DRAFT_READY", "AWAITING_FINAL_APPROVAL", "FINAL_READY",
    "PREFLIGHT_READY", "NEEDS_REMATCH",
}

TRANSITIONS = {
    "NEW": {"RESUME_READY", "JD_READY"},
    "RESUME_READY": {"JD_READY"},
    "JD_READY": {"ANALYZING"},
    "ANALYZING": {"NEEDS_CLARIFICATION", "READY_FOR_STRATEGY"},
    "NEEDS_CLARIFICATION": {"ANALYZING", "READY_FOR_STRATEGY"},
    "READY_FOR_STRATEGY": {"STRATEGY_READY"},
    "STRATEGY_READY": {"AWAITING_STRATEGY_APPROVAL", "READY_FOR_STRATEGY"},
    "AWAITING_STRATEGY_APPROVAL": {"EDITING", "READY_FOR_STRATEGY"},
    "EDITING": {"DRAFT_READY", "READY_FOR_STRATEGY"},
    "DRAFT_READY": {"AWAITING_FINAL_APPROVAL", "READY_FOR_STRATEGY", "EDITING"},
    "AWAITING_FINAL_APPROVAL": {"FINAL_READY", "READY_FOR_STRATEGY", "EDITING"},
    "FINAL_READY": {"PREFLIGHT_READY", "READY_FOR_STRATEGY"},
    "PREFLIGHT_READY": {"FINAL_READY", "READY_FOR_STRATEGY"},
    "NEEDS_REMATCH": {"ANALYZING"},
}


class InvalidTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class NextAction:
    action: str
    message: str
    reason: str


def validate_transition(current: str, target: str) -> None:
    if current not in STATES or target not in STATES:
        raise InvalidTransition("未知的Application状态")
    if target not in TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"不允许从 {current} 进入 {target}")


def transition(user_id: str, application_id: str, target: str) -> dict:
    workspace = database.get_application_workspace(user_id, application_id)
    if not workspace:
        raise PermissionError("Application不存在或不属于当前用户")
    current = workspace.get("application_status", "NEW")
    validate_transition(current, target)
    database.update_application_fields(user_id, application_id, application_status=target)
    return database.get_application_workspace(user_id, application_id)


def determine_next_action(workspace: dict) -> NextAction:
    state = workspace.get("application_status", "NEW")
    mapping = {
        "NEW": NextAction("choose_resume", "先选择一份基础简历。", "Agent需要一个结构起点。"),
        "RESUME_READY": NextAction("add_jd", "粘贴或上传目标岗位JD。", "岗位要求决定内容优先级。"),
        "JD_READY": NextAction("analyze", "开始分析岗位与现有经历。", "将生成Requirement、Gap和Eligibility。"),
        "ANALYZING": NextAction("wait", "正在分析。", "完成后会判断是否值得补充经历。"),
        "NEEDS_CLARIFICATION": NextAction("clarify_or_skip", "建议回答最多三个高价值问题，也可以跳过。", "只询问可能改变申请策略的表达缺口。"),
        "READY_FOR_STRATEGY": NextAction("build_strategy", "生成针对该岗位的简历策略。", "策略生成后仍需你批准。"),
        "STRATEGY_READY": NextAction("review_strategy", "查看策略并决定是否提交审批。", "尚未修改简历。"),
        "AWAITING_STRATEGY_APPROVAL": NextAction("approve_strategy", "批准、拒绝或补充偏好。", "批准前禁止生成Draft。"),
        "EDITING": NextAction("generate_draft", "按已批准策略生成并验证Draft。", "无事实支持的编辑会被拦截。"),
        "DRAFT_READY": NextAction("review_draft", "对照检查Base Resume与Target Draft。", "确认修改后才能进入Final。"),
        "AWAITING_FINAL_APPROVAL": NextAction("approve_final", "确认最终内容。", "未经确认不会标记Final。"),
        "FINAL_READY": NextAction("preflight", "运行投递前检查。", "检查真实性、资格风险和格式。"),
        "PREFLIGHT_READY": NextAction("export", "选择模板并导出。", "模板只改变呈现，不改变事实或评分。"),
        "NEEDS_REMATCH": NextAction("rematch", "职业资料发生变化，需要重新匹配。", "旧Analysis不会继续冒充最新结果。"),
    }
    return mapping[state]
