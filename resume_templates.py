from __future__ import annotations

import database


TEMPLATES = {
    "ats_simple": {"template_id": "ats_simple", "name": "ATS 简洁单栏", "category": "General", "description": "单栏、清晰标题、适合大多数ATS。", "recommended_roles": ["all"], "ats_friendly": True, "page_style": "single_column", "density": "standard", "source": "built_in", "preview": "clean"},
    "ai_product": {"template_id": "ai_product", "name": "AI / 产品", "category": "Product", "description": "优先展示定位、产品项目与业务能力。", "recommended_roles": ["ai", "product", "operations"], "ats_friendly": True, "page_style": "single_column", "density": "standard", "source": "built_in", "preview": "product"},
    "software_engineer": {"template_id": "software_engineer", "name": "软件工程师", "category": "Engineering", "description": "强化技术栈与项目实现。", "recommended_roles": ["software", "c++", "developer"], "ats_friendly": True, "page_style": "single_column", "density": "compact", "source": "built_in", "preview": "engineering"},
    "game_ue": {"template_id": "game_ue", "name": "游戏 / UE 开发", "category": "Game", "description": "突出UE、C++、项目模块与技术验证。", "recommended_roles": ["ue", "unreal", "game"], "ats_friendly": True, "page_style": "single_column", "density": "standard", "source": "built_in", "preview": "game"},
    "graduate": {"template_id": "graduate", "name": "应届 / 转型", "category": "Graduate", "description": "优先项目、课程与可迁移能力。", "recommended_roles": ["graduate", "junior"], "ats_friendly": True, "page_style": "single_column", "density": "standard", "source": "built_in", "preview": "graduate"},
    "compact_one_page": {"template_id": "compact_one_page", "name": "紧凑一页", "category": "Compact", "description": "缩小间距，适合内容较多的一页简历。", "recommended_roles": ["all"], "ats_friendly": True, "page_style": "single_column", "density": "compact", "source": "built_in", "preview": "compact"}
}


def list_templates() -> list[dict]:
    return list(TEMPLATES.values())


def recommend_template(target_role: str) -> dict:
    role = (target_role or "").lower()
    if any(term in role for term in ("ue", "unreal", "游戏")):
        return TEMPLATES["game_ue"]
    if any(term in role for term in ("c++", "开发", "engineer", "software")):
        return TEMPLATES["software_engineer"]
    if any(term in role for term in ("ai", "产品", "运营")):
        return TEMPLATES["ai_product"]
    if any(term in role for term in ("应届", "实习", "graduate", "junior")):
        return TEMPLATES["graduate"]
    return TEMPLATES["ats_simple"]


def select_template(user_id: str, application_id: str, template_id: str) -> dict:
    if template_id not in TEMPLATES:
        raise ValueError("未知模板")
    before = database.get_application_workspace(user_id, application_id)
    database.update_application_fields(user_id, application_id, template_id=template_id)
    after = database.get_application_workspace(user_id, application_id)
    if before.get("analysis_json") != after.get("analysis_json") or before.get("evidence_revision") != after.get("evidence_revision"):
        raise RuntimeError("模板切换不得改变Career Truth或岗位匹配")
    return TEMPLATES[template_id]
