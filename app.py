from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import uuid

import streamlit as st

import database
from identity import clear_user_session_state, resolve_user_id
from deepseek_client import (
    DeepSeekError, analyze_jd, deep_dive_priority, organize_deep_dive_case, extract_resume_evidence,
    generate_deep_dive_question, generate_polish_strategy, is_configured, propose_resume_edits,
    stabilize_incremental_analysis, sync_all_resume_evidence,
)
from jd_ocr import OCRError, extract_jd_text
from jd_fetch import JDFetchError, fetch_jd_text
from resume_editor import validate_and_apply_edits
from resume_import import extract_resume_text, save_original_file
from application_workflow import transition
from career_agent import analyze_application, approve_final_resume, approve_strategy, review_draft
from preflight import run_preflight
from resume_export import export_application_docx
from resume_templates import list_templates, recommend_template, select_template
from scoring import analysis_delta, application_advice, calculate_eligibility, calculate_scores
from unified_profile import build_experience_blocks, canonical_evidence_pool, resolved_fact_controls


st.set_page_config(page_title="Career Agent V0.3 Beta", page_icon="📄", layout="wide")
database.init_db()
requested_user_id = st.query_params.get("user_id")
current_user_id = resolve_user_id(requested_user_id)
if not current_user_id:
    st.error("这个 Beta 邀请链接无效或用户已停用。请向管理员获取有效邀请链接。")
    st.stop()
clear_user_session_state(st.session_state, st.session_state.get("current_user_id"), current_user_id)
APP_DEBUG = os.getenv("APP_DEBUG", "false").lower() in {"1", "true", "yes"}

STATUS_CN = {
    "direct_strong": "直接强匹配", "direct_partial": "直接部分匹配", "transferable_match": "可迁移匹配",
    "strong_match": "直接强匹配", "partial_match": "直接部分匹配", "no_evidence": "缺少证据",
    "not_met": "明确不满足", "needs_confirmation": "待确认",
}
PRIORITY_CN = {
    "must_have": "Must-have", "preferred": "Preferred",
    "core_responsibility": "Core Responsibility", "supporting": "辅助要求",
}


def resume_options():
    resumes = database.list_resumes(current_user_id)
    return resumes, {r["id"]: f"{r['name']} · {r['id']}" for r in resumes}


def render_insights(title: str, items: list, facts: list[dict]) -> None:
    st.subheader(title)
    fact_map = {fact["id"]: fact for fact in facts}
    for item in items:
        if isinstance(item, str):
            item = {"title": item, "detail": "", "fact_ids": []}
        st.markdown(f"• **{item.get('title') or item.get('text', '')}**")
        if item.get("detail"):
            st.write(item["detail"])
        cited = [fact_map[fid] for fid in item.get("fact_ids", []) if fid in fact_map]
        if cited:
            with st.expander("查看依据"):
                for fact in cited:
                    st.write((f"**{fact['id']}** · " if APP_DEBUG else "") + fact["statement"])
                    for source in fact.get("sources", []):
                        st.caption(f"Resume Source：{source.get('resume_id') or '用户确认'}｜{source.get('source_excerpt', '')}")


st.title("Career Agent · V0.3 Beta")
st.caption("管理你的简历，理解目标岗位，并在真实经历范围内完成针对性润色。")
with st.expander("Beta 数据与隐私提示"):
    st.write("岗位分析和简历润色所需的 Resume、JD、已确认职业证据及 Deep Dive 文本会发送给当前配置的第三方模型 Provider。")
    st.write("Resume 文件、Career Evidence、SQLite 应用数据和本地 OCR 结果保存在 Beta 服务器；本地/服务器 OCR 不会发送给额外 OCR 服务。")
    st.write("这是测试版本，请不要上传不希望由第三方模型处理的信息。")
if is_configured():
    st.success("AI 模型 API 已配置", icon="✅")
else:
    st.warning("尚未完整配置当前 AI Provider。请检查服务器环境变量。", icon="⚠️")

resume_tab, applications_tab, jd_tab, optimize_tab = st.tabs(["我的职业资料", "我的申请", "新建申请", "简历与导出"])

with resume_tab:
    st.subheader("我的简历")
    st.write("上传或粘贴你真实使用的简历。简历明确写出的内容直接作为User-Declared Evidence；AI推断内容仍需确认。")
    if message := st.session_state.pop("resume_flash", None):
        st.success(message)
    with st.expander("添加一份简历", expanded=not bool(database.list_resumes(current_user_id))):
        with st.form("add_resume_form", clear_on_submit=False):
            name = st.text_input("简历名称 *", placeholder="例如：UE / C++ 开发简历", key="add_resume_name")
            language = st.selectbox("简历语言", ["auto", "中文", "English"], key="add_resume_language")
            upload = st.file_uploader("上传 DOCX 或 TXT", type=["docx", "txt"], key="add_resume_upload")
            pasted = st.text_area("或者粘贴简历内容", height=220, key="add_resume_text")
            if st.form_submit_button("添加到我的简历", type="primary"):
                resume_id = None
                saved_path = None
                try:
                    file_bytes = upload.getvalue() if upload else b""
                    content = extract_resume_text(upload.name, file_bytes) if upload else pasted.strip()
                    if not name.strip() or not content:
                        st.error("请填写简历名称，并上传文件或粘贴内容。")
                    else:
                        resume_id = database.add_resume(current_user_id, name, content, language, upload.name if upload else "")
                        if upload:
                            saved_path = save_original_file(current_user_id, resume_id, upload.name, file_bytes)
                            database.update_resume(current_user_id, resume_id, name, content, language, upload.name, saved_path)
                        if not database.get_resume(current_user_id, resume_id):
                            raise RuntimeError("SQLite 保存后未能回读该简历。")
                        st.session_state["manage_resume_id"] = resume_id
                        st.success(f"简历已添加（{resume_id}），输入区内容已保留。")
                except Exception as exc:
                    # A partially-created row/file must not survive a failed upload,
                    # while widget-backed session state remains untouched for retry.
                    if resume_id:
                        try:
                            database.delete_resume(current_user_id, resume_id)
                        except Exception:
                            pass
                    if saved_path:
                        Path(saved_path).unlink(missing_ok=True)
                    st.error(f"导入失败：{exc}")

    resumes, resume_labels = resume_options()
    if not resumes:
        st.info("还没有简历。请先添加一份真实简历。")
    else:
        selected_id = st.selectbox("选择简历", [r["id"] for r in resumes], format_func=lambda rid: resume_labels[rid], key="manage_resume_id")
        selected = database.get_resume(current_user_id, selected_id)
        with st.form("edit_resume_asset"):
            edited_name = st.text_input("简历名称", value=selected["name"], key=f"edit_resume_name_{selected_id}")
            languages = ["auto", "中文", "English"]
            edited_language = st.selectbox("语言", languages, index=languages.index(selected["language"]) if selected["language"] in languages else 0, key=f"edit_resume_language_{selected_id}")
            edited_content = st.text_area("简历内容", value=selected["content_text"], height=420, key=f"edit_resume_content_{selected_id}")
            if st.form_submit_button("保存修改"):
                try:
                    if not edited_name.strip() or not edited_content.strip():
                        st.error("简历名称和内容不能为空。")
                    else:
                        database.update_resume(current_user_id, selected_id, edited_name, edited_content, edited_language)
                        st.session_state["resume_flash"] = "简历已保存。已有确认事实不会被自动改写；内容变化较大时请重新提取。"
                        st.rerun()
                except Exception as exc:
                    st.error(f"保存失败：{exc}")

        c1, c2 = st.columns([2, 1])
        if c1.button("整理 / 更新我的经历", disabled=not is_configured(), type="primary"):
            with st.spinner("正在整理简历中明确写出的经历，并识别需要补充说明的内容……"):
                try:
                    extracted = extract_resume_evidence(
                        selected["name"], selected["content_text"], user_id=current_user_id
                    )
                    declared_count = candidate_count = 0
                    for evidence in extracted:
                        evidence.update({"source": f"简历：{selected['name']}", "source_resume_id": selected_id})
                        if evidence["evidence_origin"] == "resume_declared":
                            _, created = database.add_or_merge_resume_fact(current_user_id, evidence, selected_id)
                            declared_count += int(created)
                        else:
                            _, created = database.add_or_reuse_candidate_fact(current_user_id, evidence, selected_id)
                            candidate_count += int(created)
                    st.success(f"已整理 {declared_count} 条简历经历；另有 {candidate_count} 条信息需要你补充说明。")
                    st.rerun()
                except DeepSeekError as exc:
                    st.error(str(exc))
        if c2.button("删除这份简历"):
            st.session_state["confirm_delete_resume"] = selected_id
        if st.session_state.get("confirm_delete_resume") == selected_id:
            st.warning("删除简历不会删除已确认职业事实，但会解除来源简历关联。确定删除吗？")
            if st.button("确定删除", type="primary"):
                database.delete_resume(current_user_id, selected_id)
                st.session_state.pop("confirm_delete_resume", None)
                st.session_state.pop("manage_resume_id", None)
                st.rerun()

        pending = [f for f in database.list_facts(current_user_id) if f.get("source_resume_id") == selected_id and f["verification_status"] == "needs_confirmation"]
        if pending:
            st.divider()
            st.subheader(f"完善职业信息（{len(pending)}）")
            st.caption("以下内容不是简历原文明示的信息，只有你确认后才会加入我的经历。")
            for fact in pending:
                with st.container(border=True):
                    st.write("请补充或修正这条经历：")
                    if fact.get("source_excerpt"):
                        st.caption("简历原文：" + fact["source_excerpt"])
                    edited_statement = st.text_area("经历描述", value=fact["statement"], key=f"statement-{fact['id']}")
                    edited_org, edited_title = fact.get("organization", ""), fact.get("official_job_title", "")
                    if fact.get("evidence_type") == "Employment":
                        ec1, ec2 = st.columns(2)
                        edited_org = ec1.text_input("公司 / 组织", value=edited_org, key=f"org-{fact['id']}")
                        edited_title = ec2.text_input("正式 Job Title", value=edited_title, key=f"title-{fact['id']}")
                    edited_skills = st.text_input("技能（逗号分隔）", value=", ".join(fact["skills"]), key=f"skills-{fact['id']}")
                    yes, no, later = st.columns(3)
                    if yes.button("是，确认", key=f"confirm-{fact['id']}", type="primary"):
                        identity_ok = fact.get("evidence_type") != "Employment" or (edited_org.strip() and edited_title.strip())
                        if edited_statement.strip() and identity_ok:
                            database.confirm_edited_fact(
                                current_user_id, fact["id"], edited_org, edited_title, edited_statement,
                                [x.strip() for x in edited_skills.replace("，", ",").split(",") if x.strip()],
                                fact.get("restrictions", ""),
                            )
                            st.rerun()
                        else:
                            st.error("经历描述不能为空；工作经历还必须包含公司和正式 Job Title。")
                    if no.button("不是", key=f"reject-{fact['id']}"):
                        database.update_fact_status(current_user_id, fact["id"], "rejected")
                        st.rerun()
                    later.button("稍后处理", key=f"later-{fact['id']}", disabled=True)

        with st.expander("查看我的职业资料概览"):
            blocks = build_experience_blocks(database.list_facts(current_user_id, confirmed_only=True))
            for block in blocks:
                st.markdown(f"**{block['heading']} · {block['category']}**")
                for evidence in block["evidence"]:
                    st.write("- " + evidence["fact"].get("statement", ""))


with applications_tab:
    st.subheader("我的申请")
    applications = database.list_applications(current_user_id)
    if not applications:
        st.info("还没有申请。请到“新建申请”输入目标岗位。")
    for application in applications:
        with st.container(border=True):
            left, middle, right = st.columns([3, 2, 1])
            left.markdown(f"**{application['company']} · {application['job_title']}**")
            middle.write(application["application_status"].replace("_", " ").title())
            if right.button("继续", key=f"open-app-{application['id']}"):
                workspace = database.get_application_workspace(current_user_id, application["id"])
                st.session_state.update(
                    application_id=workspace["id"], analysis=workspace.get("analysis") or {},
                    analysis_jd=workspace["jd_text"], active_base_resume_id=workspace["base_resume_id"],
                    resume_draft=workspace.get("resume") or None,
                    polish_strategy=workspace.get("strategy") or None,
                )
                st.success("已加载该申请，请到“简历与导出”继续。")


with jd_tab:
    st.subheader("新建申请 · 岗位分析")
    resumes, resume_labels = resume_options()
    confirmed_facts = database.list_facts(current_user_id, confirmed_only=True)
    if not resumes:
        st.info("请先在“我的简历”中添加至少一份简历。")
    else:
        base_id = st.selectbox("选择 Base Resume *", [r["id"] for r in resumes], format_func=lambda rid: resume_labels[rid], key="analysis_base_resume_id")
        base_resume = database.get_resume(current_user_id, base_id)
        with st.expander("查看 Base Resume"):
            st.text(base_resume["content_text"])
        c1, c2 = st.columns(2)
        job_title = c1.text_input("目标岗位名称", key="job_title")
        company = c2.text_input("目标公司", key="company")
        jd_url = st.text_input("职位链接（可选，提取后仍需你审核）", key="jd_url")
        if st.button("从职位链接提取", disabled=not bool(jd_url.strip())):
            try:
                st.session_state["jd_text"] = fetch_jd_text(jd_url.strip())
                st.success("已提取文字，请在下方审核后再分析。")
            except JDFetchError as exc:
                st.warning(str(exc))
        screenshots = st.file_uploader(
            "可选：上传同一岗位的JD截图（按文件顺序合并）",
            type=["png", "jpg", "jpeg"], accept_multiple_files=True, key="jd_screenshots",
        )
        if st.button("从截图提取文字到JD文本", disabled=not bool(screenshots)):
            with st.spinner("正在本地识别截图文字……"):
                try:
                    extracted_jd = extract_jd_text([(file.name, file.getvalue()) for file in screenshots])
                    st.session_state["jd_text"] = extracted_jd
                    st.success("文字已提取。请检查和修改后，再开始岗位分析。")
                    st.rerun()
                except OCRError as exc:
                    st.error(str(exc))
        jd_text = st.text_area("User-Reviewed JD Text（可粘贴或修改OCR结果）", height=300, key="jd_text")
        st.caption(f"当前统一职业证据池：{len(confirmed_facts)} 条。分析时会先同步所有 Resume；Base Resume 只决定草稿结构。")
        if st.button("开始岗位分析", type="primary", disabled=not is_configured()):
            if not jd_text.strip():
                st.error("请先粘贴 JD。")
            else:
                with st.spinner("正在同步全部 Resume，并用统一职业证据池分析 JD……"):
                    try:
                        sync_result = sync_all_resume_evidence(current_user_id, database.list_resumes(current_user_id), database)
                        raw_confirmed_facts = database.list_facts(current_user_id, confirmed_only=True)
                        confirmed_facts = canonical_evidence_pool(raw_confirmed_facts)
                        if not confirmed_facts:
                            st.error("全部 Resume 中仍未提取到可用 Evidence。")
                            st.stop()
                        pending_id = st.session_state.get("pending_application_id")
                        pending = database.get_application_workspace(current_user_id, pending_id) if pending_id else None
                        if not pending or pending.get("jd_text") != jd_text or pending.get("base_resume_id") != base_id:
                            app_id = database.create_application_workspace(
                                current_user_id, job_title or "未命名岗位", company or "未填写公司",
                                jd_text, base_id, "url" if jd_url.strip() else ("ocr" if screenshots else "text"), jd_url.strip(),
                            )
                            st.session_state["pending_application_id"] = app_id
                        else:
                            app_id = pending_id
                        workspace = analyze_application(current_user_id, app_id)
                        analysis = workspace["analysis"]
                        st.session_state.update(analysis=analysis, application_id=app_id, analysis_jd=jd_text, active_base_resume_id=base_id)
                        st.session_state.pop("pending_application_id", None)
                        st.success(f"分析完成。同步 Resume {sync_result['parsed_resumes']} 份；统一证据池 {len(confirmed_facts)} 条。")
                    except DeepSeekError as exc:
                        st.error(str(exc))

    analysis = st.session_state.get("analysis")
    if analysis:
        requirements = analysis["requirements"]
        scores = calculate_scores(requirements)
        eligibility = calculate_eligibility(requirements)
        advice, advice_reason = application_advice(requirements, scores, eligibility)
        st.divider()
        m1, m2, m3 = st.columns(3)
        m1.metric("岗位适配评分", f"{scores.match_score} / 90")
        m2.metric("Evidence Coverage", f"{scores.evidence_coverage}%")
        m3.metric("Eligibility", eligibility)
        st.caption("岗位适配评分仅用于比较当前职业资料与目标岗位的适配程度，不代表获得面试或录用的概率。Eligibility始终独立判断。")
        st.info(f"**求职建议：{advice}**\n\n{advice_reason}")
        groups = ["direct_strong", "direct_partial", "transferable_match", "no_evidence", "not_met", "needs_confirmation"]
        for col, status in zip(st.columns(6), groups):
            count = sum(1 for r in requirements if r.get("nature") == "match" and r.get("match_status") == status)
            col.metric(STATUS_CN[status], count)

        st.subheader("逐项解释")
        for req in requirements:
            if req["nature"] == "eligibility":
                state = {"met": "满足", "not_met": "明确不满足", "needs_confirmation": "待确认"}.get(req["eligibility_status"], "待确认")
                label = f"{req['id']} · Eligibility · {state} · {req['text']}"
            else:
                label = f"{req['id']} · {PRIORITY_CN[req['priority']]} · 权重 {req['weight']} · {STATUS_CN[req['match_status']]} · {req['text']}"
            with st.expander(label):
                st.write(req["reason"] or "暂无说明")
                if req.get("criteria"):
                    st.write("**Criteria：** " + "；".join(req["criteria"]))
                if req.get("critical_weight_suggested"):
                    st.info("AI 认为该要求可能值得权重4；当前未获人工确认，仍按基础权重计算。")
                st.caption("证据：" + (", ".join(req["fact_ids"]) or "无已确认事实证据"))
                if req.get("original_text"):
                    st.caption("JD 依据：" + req["original_text"])

        expression_gaps = [r for r in requirements if r.get("gap_type") == "expression_gap"]
        real_gaps = [r for r in requirements if r.get("gap_type") == "real_gap"]
        qualification_gaps = [r for r in requirements if r.get("gap_type") == "qualification_gap"]
        gap_cols = st.columns(3)
        with gap_cols[0]:
            st.subheader("表达缺口")
            if expression_gaps:
                for req in expression_gaps:
                    st.write("- " + req["text"])
            else:
                st.caption("未发现明显表达缺口")
        with gap_cols[1]:
            st.subheader("真实缺口")
            if real_gaps:
                for req in real_gaps:
                    st.write("- " + req["text"])
            else:
                st.caption("未发现明确真实缺口")
        with gap_cols[2]:
            st.subheader("硬资格")
            if qualification_gaps:
                for req in qualification_gaps:
                    state = req.get("eligibility_status") or "待确认"
                    st.write(f"- {req['text']}（{state}）")
            else:
                st.caption("未发现明确硬资格项")

        missing = sorted([
            r for r in requirements
            if (r.get("nature") == "match" and r.get("match_status") in {"no_evidence", "needs_confirmation", "transferable_match", "direct_partial"})
            or (r.get("nature") == "eligibility" and r.get("eligibility_status") == "needs_confirmation")
        ], key=deep_dive_priority, reverse=True)
        missing = [req for req in missing if deep_dive_priority(req) >= 50][:5]
        if missing:
            st.subheader("经历深挖")
            st.info("缺少证据不代表你不会。系统会结合现有经历寻找相邻场景和可迁移能力，不会诱导你声称完全满足JD。")
            chosen_req_id = st.selectbox("选择要回答的要求", [r["id"] for r in missing], format_func=lambda rid: next(r["text"] for r in missing if r["id"] == rid))
            chosen_req = next(r for r in missing if r["id"] == chosen_req_id)
            if st.button("生成针对已有经历的深挖问题", type="primary"):
                with st.spinner("正在结合你的多份简历生成经历深挖问题……"):
                    try:
                        dive = generate_deep_dive_question(
                            chosen_req, database.list_resumes(current_user_id),
                            database.list_facts(current_user_id, confirmed_only=True), user_id=current_user_id,
                        )
                        dive["requirement_id"] = chosen_req_id
                        st.session_state["deep_dive"] = dive
                    except DeepSeekError as exc:
                        st.error(str(exc))

            dive = st.session_state.get("deep_dive")
            if dive and dive.get("requirement_id") == chosen_req_id:
                st.write("**深挖问题：** " + dive["question"])
                if dive.get("why_ask"):
                    with st.expander("为什么问这个？"):
                        st.write(dive["why_ask"])
                with st.form("deep_dive_answer_form", clear_on_submit=False):
                    free_answer = st.text_area(
                        "请自由描述你的实际情况",
                        placeholder="可以说明实际规模、做过的部分、没有的权限、与JD的差距，以及一个真实案例。",
                        height=160,
                        key=f"deep_dive_answer_{chosen_req_id}",
                    )
                    if st.form_submit_button("整理为待补充经历", type="primary"):
                        if not free_answer.strip():
                            st.error("请先填写你的实际情况。")
                        else:
                            try:
                                case_bundle = organize_deep_dive_case(
                                    chosen_req,
                                    dive["question"],
                                    free_answer,
                                    database.list_resumes(current_user_id),
                                    database.list_facts(current_user_id, confirmed_only=True),
                                    user_id=current_user_id,
                                )
                                case_bundle["source_answer"] = free_answer
                                case_bundle["requirement_id"] = chosen_req_id
                                st.session_state["deep_dive_case"] = case_bundle
                                st.rerun()
                            except DeepSeekError as exc:
                                st.error(str(exc))

            case_bundle = st.session_state.get("deep_dive_case")
            if case_bundle:
                case = case_bundle["case"]
                st.write("**请确认这段完整经历。确认后，系统会在后台建立可追溯证据并重新匹配。**")
                with st.container(border=True):
                    title = st.text_input("经历名称", value=case.get("title", ""), key="case-title")
                    summary = st.text_area("真实经历概述", value=case.get("summary", ""), height=150, key="case-summary")
                    skills = st.text_input("涉及技能", value=", ".join(case.get("skills", [])), key="case-skills")
                    organization = official_title = project_name = role = ""
                    if case.get("case_type") == "Employment":
                        c1, c2 = st.columns(2)
                        organization = c1.text_input("公司 / 组织", value=case.get("organization", ""), key="case-org")
                        official_title = c2.text_input("正式 Job Title", value=case.get("official_job_title", ""), key="case-job-title")
                    elif case.get("case_type") == "Project":
                        c1, c2 = st.columns(2)
                        project_name = c1.text_input("项目名称", value=case.get("project_name", ""), key="case-project")
                        role = c2.text_input("你在项目中的角色", value=case.get("role", ""), key="case-role")
                    confirm_col, later_col = st.columns(2)
                    if confirm_col.button("确认并保存这段经历", type="primary"):
                        if not title.strip() or not summary.strip() or (case.get("case_type") == "Employment" and (not organization.strip() or not official_title.strip())):
                            st.error("请补全经历名称、概述；工作经历还必须填写公司和正式 Job Title。")
                        else:
                            confirmed_case = {**case, "title": title, "summary": summary,
                                "skills": [x.strip() for x in skills.replace("，", ",").split(",") if x.strip()],
                                "organization": organization, "official_job_title": official_title,
                                "project_name": project_name or case.get("project_name", ""), "role": role or case.get("role", ""),
                                "source_answer": case_bundle["source_answer"], "requirement_id": chosen_req_id}
                            case_id = database.add_experience_case(current_user_id, confirmed_case)
                            for evidence in case_bundle["evidence"]:
                                if evidence.get("evidence_type") != "Employment":
                                    evidence["organization"] = evidence["official_job_title"] = ""
                                database.add_or_merge_user_confirmed_fact(current_user_id, {**evidence, "case_id": case_id, "context": summary,
                                    "project_name": project_name or evidence.get("project_name", ""), "role": role or evidence.get("role", ""),
                                    "organization": organization if evidence.get("evidence_type") == "Employment" else "",
                                    "official_job_title": official_title if evidence.get("evidence_type") == "Employment" else "",
                                    "verification_status": "confirmed", "evidence_origin": "user_confirmed",
                                    "source": f"用户确认经历：{case_id}"})
                            before = st.session_state["analysis"]
                            try:
                                current_evidence = canonical_evidence_pool(database.list_facts(current_user_id, confirmed_only=True))
                                refreshed = analyze_jd(
                                    st.session_state["analysis_jd"], current_evidence, user_id=current_user_id
                                )
                                refreshed = stabilize_incremental_analysis(before, refreshed, current_evidence)
                                st.session_state["analysis"] = refreshed
                                st.session_state["rematch_delta"] = analysis_delta(before, refreshed)
                                database.update_application_analysis(current_user_id, st.session_state["application_id"], refreshed)
                                database.update_application_fields(
                                    current_user_id, st.session_state["application_id"],
                                    application_status="READY_FOR_STRATEGY",
                                    evidence_revision=database.get_user(current_user_id)["career_revision"],
                                    requirement_state_json={"requirements": refreshed.get("requirements", [])},
                                )
                            except DeepSeekError as exc:
                                st.session_state["rematch_error"] = f"经历已保存，但重新匹配失败：{exc}"
                            st.session_state.pop("deep_dive_case", None)
                            st.rerun()
                    if later_col.button("稍后处理"):
                        st.session_state.pop("deep_dive_case", None)
                        st.rerun()
            if delta := st.session_state.pop("rematch_delta", None):
                st.success(f"已重新匹配：当前证据匹配度 {delta['score_before']} → {delta['score_after']}；证据覆盖 {delta['coverage_before']}% → {delta['coverage_after']}%。")
                for item in delta["changed"]:
                    st.write(f"- {item['text']}：{STATUS_CN.get(item['before'], item['before'])} → {STATUS_CN.get(item['after'], item['after'])}")
            if error := st.session_state.pop("rematch_error", None):
                st.warning(error)

        left, right = st.columns(2)
        with left:
            render_insights("主要优势", analysis.get("strengths", []), database.list_facts(current_user_id, confirmed_only=True))
        with right:
            render_insights("主要短板", analysis.get("weaknesses", []), database.list_facts(current_user_id, confirmed_only=True))

with optimize_tab:
    st.subheader("针对职位润色简历")
    st.warning("AI 润色草稿 / 待人工审核", icon="⚠️")
    analysis = st.session_state.get("analysis")
    base_id = st.session_state.get("active_base_resume_id")
    if not analysis or not base_id:
        st.info("请先在“岗位分析”中选择 Base Resume 并完成一次分析。")
    else:
        base_resume = database.get_resume(current_user_id, base_id)
        st.write(f"**Base Resume：{base_resume['name']}**")
        st.caption("润色从 Base Resume 出发，并可使用其他简历中的真实经历；不会覆盖原始简历。")
        all_generation_facts = database.list_facts(current_user_id, confirmed_only=True, generation_only=True)
        used_fact_ids = {
            fact_id for req in analysis["requirements"]
            for fact_id in req.get("fact_ids", [])
        }
        preference = st.text_area(
            "本次润色要求（可选）",
            placeholder="例如：控制在一页；重点突出AI项目；保留UE开发背景；表达更偏产品经理；不要删除IGG经历……",
            key=f"polish-preference-{st.session_state['application_id']}",
        )
        st.caption("本次要求只影响当前岗位，不能绕过真实性规则或内容选择。")
        st.markdown("### 本次简历内容选择")
        st.caption("默认按完整经历选择；展开后可给单条内容设置例外。单条控制优先于整段经历控制。")
        controls = {}
        control_options = ["include", "de_emphasize", "skip", "must_include"]
        control_labels = {
            "include": "使用", "de_emphasize": "弱化", "skip": "跳过", "must_include": "必须加入",
        }
        blocks = build_experience_blocks(all_generation_facts)
        application_id = st.session_state["application_id"]
        for block in blocks:
            block_fact_ids = {fid for item in block["evidence"] for fid in item["fact_ids"]}
            default = "include" if block_fact_ids & used_fact_ids else "de_emphasize"
            choice = st.selectbox(
                f"{block['heading']}（{block['category']}）",
                control_options, index=control_options.index(default),
                format_func=lambda value: control_labels[value],
                key=f"block-control-{application_id}-{block['id']}",
            )
            controls[f"block:{block['id']}"] = choice
            with st.expander(f"展开详情 · {len(block['evidence'])} 条内容"):
                for evidence in block["evidence"]:
                    fact = evidence["fact"]
                    st.write(fact["statement"])
                    source_count = len({s.get("resume_id") for s in evidence["sources"] if s.get("resume_id")})
                    if source_count:
                        st.caption(f"来自 {source_count} 份 Resume")
                    options = ["follow"] + control_options
                    override = st.selectbox(
                        "单条内容控制", options,
                        format_func=lambda value: "跟随整段经历" if value == "follow" else control_labels[value],
                        key=f"evidence-control-{application_id}-{evidence['fact_ids'][0]}",
                        label_visibility="collapsed",
                    )
                    if override != "follow":
                        controls[f"evidence:{evidence['fact_ids'][0]}"] = override
        fact_controls = resolved_fact_controls(blocks, controls)
        strategy_signature = hashlib.sha256(json.dumps({
            "application_id": application_id,
            "controls": fact_controls,
            "preference": preference,
            "analysis": analysis,
            "blocks": [{"id": b["id"], "heading": b["heading"], "category": b["category"]} for b in blocks],
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if st.button("生成本次润色策略", type="primary", disabled=not is_configured()):
            database.update_content_controls(current_user_id, application_id, controls)
            database.update_user_preference(current_user_id, application_id, preference)
            strategy_facts = [fact for fact in all_generation_facts if fact_controls.get(fact["id"]) != "skip"]
            with st.spinner("正在生成简短的润色策略……"):
                try:
                    strategy = generate_polish_strategy(
                        base_resume, st.session_state["analysis_jd"], analysis,
                        strategy_facts, fact_controls, preference, blocks, user_id=current_user_id,
                    )
                    st.session_state["polish_strategy"] = strategy
                    st.session_state["polish_strategy_signature"] = strategy_signature
                    workspace = database.get_application_workspace(current_user_id, application_id)
                    database.update_application_fields(current_user_id, application_id, strategy_json=strategy)
                    if workspace["application_status"] == "NEEDS_CLARIFICATION":
                        transition(current_user_id, application_id, "READY_FOR_STRATEGY")
                        workspace["application_status"] = "READY_FOR_STRATEGY"
                    if workspace["application_status"] == "READY_FOR_STRATEGY":
                        transition(current_user_id, application_id, "STRATEGY_READY")
                        transition(current_user_id, application_id, "AWAITING_STRATEGY_APPROVAL")
                    st.rerun()
                except DeepSeekError as exc:
                    st.error(str(exc))

        strategy = st.session_state.get("polish_strategy")
        strategy_is_current = strategy and st.session_state.get("polish_strategy_signature") == strategy_signature
        if strategy_is_current:
            st.markdown("### 本次润色策略")
            strategy_cols = st.columns(2)
            with strategy_cols[0]:
                st.markdown("**重点突出**")
                for item in strategy.get("highlight", []):
                    st.write("- " + item)
                st.markdown("**适当弱化**")
                for item in strategy.get("de_emphasize", []):
                    st.write("- " + item)
            with strategy_cols[1]:
                st.markdown("**不会包装**")
                for item in strategy.get("never_claim", []):
                    st.write("- " + item)
                st.markdown("**建议补充**")
                for item in strategy.get("suggest_clarification", []):
                    st.write("- " + item)
            if strategy.get("summary"):
                st.caption(strategy["summary"])
            if strategy.get("headline"):
                st.markdown(f"**建议定位：** {strategy['headline']}")
            if strategy.get("block_ranking"):
                st.markdown("**经历与目标岗位的相关性**")
                block_names = {block["id"]: block["heading"] for block in blocks}
                for item in strategy["block_ranking"]:
                    st.write(f"- {item['relevance']} · {block_names.get(item['block_id'], '经历')}：{item['recommended_action']} — {item['reason']}")
            if st.button("确认策略并生成润色 Draft", type="primary"):
                database.update_content_controls(current_user_id, application_id, controls)
                database.update_user_preference(current_user_id, application_id, preference)
                planner_facts = [fact for fact in all_generation_facts if fact_controls.get(fact["id"]) != "skip"]
                with st.spinner("DeepSeek 正在提出润色方案，程序随后验证每项修改……"):
                    try:
                        workspace = database.get_application_workspace(current_user_id, application_id)
                        if workspace["application_status"] == "AWAITING_STRATEGY_APPROVAL":
                            approve_strategy(current_user_id, application_id)
                        plan = propose_resume_edits(
                            base_resume, st.session_state["analysis_jd"], analysis, planner_facts,
                            fact_controls, preference, strategy, user_id=current_user_id,
                        )
                        result = validate_and_apply_edits(
                            base_resume["content_text"], plan["edits"], all_generation_facts,
                            analysis["requirements"], fact_controls, base_id,
                        )
                        draft = {
                            "status": "draft", "base_resume_id": base_id, "content": result.content,
                            "edit_summary": plan["summary"], "polish_strategy": strategy,
                            "user_preference": preference, "applied_edits": result.applied,
                            "rejected_edits": result.rejected, "content_controls": controls,
                            "resolved_fact_controls": fact_controls, "control_notes": result.control_notes,
                        }
                        st.session_state["resume_draft"] = draft
                        database.save_resume(current_user_id, application_id, draft)
                        if database.get_application_workspace(current_user_id, application_id)["application_status"] == "EDITING":
                            transition(current_user_id, application_id, "DRAFT_READY")
                        st.success(f"已执行 {len(result.applied)} 项真实可追溯的调整，拒绝 {len(result.rejected)} 项超出事实的修改。")
                    except DeepSeekError as exc:
                        st.error(str(exc))
        elif strategy:
            st.info("润色要求或内容选择已变化，请重新生成本次润色策略。")

        draft = st.session_state.get("resume_draft")
        if draft:
            if draft.get("edit_summary"):
                st.info("润色说明：" + draft["edit_summary"])
            original_col, draft_col = st.columns(2)
            with original_col:
                st.markdown("### Base Resume 原文")
                st.text_area("原简历", value=base_resume["content_text"], height=550, disabled=True, label_visibility="collapsed")
            with draft_col:
                st.markdown("### AI 润色草稿 / 待人工审核")
                st.text_area("润色草稿", value=draft["content"], height=550, key="draft_review_text", label_visibility="collapsed")
            st.markdown("### 本次主要调整")
            for index, edit in enumerate(draft["applied_edits"], 1):
                st.write(f"{index}. {edit.get('modification_reason') or edit.get('action', '调整简历内容')}")
            with st.expander("查看依据"):
                for index, edit in enumerate(draft["applied_edits"], 1):
                    st.markdown(f"**{index}. {edit['action']} · {edit.get('target_section', '')}**")
                    if APP_DEBUG:
                        st.caption("Fact IDs：" + (", ".join(edit.get("fact_ids", [])) or "无") + "｜Requirement IDs：" + (", ".join(edit.get("requirement_ids", [])) or "无"))
                    else:
                        st.caption("这项修改来自你已确认的职业资料，并针对当前岗位要求。")
                    if edit.get("original_text"):
                        st.code(edit["original_text"])
                    if edit.get("proposed_text"):
                        st.code(edit["proposed_text"])
            if draft["rejected_edits"]:
                with st.expander(f"被程序拒绝的修改（{len(draft['rejected_edits'])}）"):
                    for rejected in draft["rejected_edits"]:
                        st.error(rejected["reason"])
            if draft.get("control_notes"):
                with st.expander("内容控制执行记录"):
                    for note in draft["control_notes"]:
                        st.write("- " + note)
            st.error("当前结果只能是 Draft。尚未经过你的真实性审核和明确确认，不能标记为 Final。")
            st.download_button("下载 Draft 文本", data=st.session_state.get("draft_review_text", draft["content"]), file_name="resume_draft.txt", mime="text/plain")
            workspace = database.get_application_workspace(current_user_id, application_id)
            if workspace["application_status"] == "DRAFT_READY":
                if st.button("我已检查内容，提交最终确认", type="primary"):
                    review_draft(current_user_id, application_id, st.session_state.get("draft_review_text", draft["content"]))
                    st.rerun()
            elif workspace["application_status"] == "AWAITING_FINAL_APPROVAL":
                st.warning("请确认：这份内容准确反映你的真实经历。")
                if st.button("确认 Final Resume", type="primary"):
                    approve_final_resume(current_user_id, application_id)
                    st.rerun()
            elif workspace["application_status"] in {"FINAL_READY", "PREFLIGHT_READY"}:
                st.success("Final Resume 已由你确认。")
                if workspace["application_status"] == "FINAL_READY" and st.button("运行投递前检查"):
                    run_preflight(current_user_id, application_id)
                    st.rerun()
                workspace = database.get_application_workspace(current_user_id, application_id)
                if workspace.get("preflight"):
                    st.write("**投递前检查：** " + workspace["preflight"]["status"])
                    for issue in workspace["preflight"].get("issues", []):
                        st.write(f"- {issue['message']}")
                templates = list_templates()
                recommended = recommend_template(workspace["job_title"])
                template_id = st.selectbox(
                    "简历模板", [item["template_id"] for item in templates],
                    index=[item["template_id"] for item in templates].index(workspace.get("template_id", recommended["template_id"])),
                    format_func=lambda tid: next(item["name"] for item in templates if item["template_id"] == tid),
                )
                if template_id != workspace.get("template_id"):
                    select_template(current_user_id, application_id, template_id)
                    st.rerun()
                export_path, export_bytes = export_application_docx(current_user_id, application_id)
                st.download_button("下载可投递 DOCX", export_bytes, file_name=Path(export_path).name,
                                   mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
