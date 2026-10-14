import json
import os

os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

import hashlib
import uuid

import streamlit as st
from auth import require_login, change_password
from utils.ids import new_uuid, build_source
from utils.logger import logger
from langchain_core.documents import Document

from services import file_state, pdf_service, vector_service, retrieve_service, rag_service, es_service
from services import eval_service, redis_service, query_rewrite_service, feedback_service

st.set_page_config(page_title="多租户 RAG 助手", page_icon="📚")

# 确保 ES 索引存在。
# 🚨 必须包 try：以前这里是裸调用，ES 没起来时整个 app 在 import 阶段就抛异常，
#    用户只能看到一大片 traceback，完全不知道是哪个依赖服务出了问题。
try:
    es_service.init_es_index()
    ES_READY, ES_ERROR = True, None
except Exception as _es_err:
    ES_READY, ES_ERROR = False, str(_es_err)

# ================= 登录与鉴权 =================
user = require_login()
user_uuid = user["user_uuid"]
is_admin = user["role"] == "admin"

# 🚨 修复：针对“重新打开页面导致继承历史”的精准清理逻辑
# 使用 session_state 标记是否为新会话
if "session_initialized" not in st.session_state:
    # 清理该用户的私人库历史
    redis_service.clear_history(user_uuid, f"private:{user_uuid}")
    # 如果是管理员，顺带清理公共库历史（保留语义缓存）
    if is_admin:
        redis_service.clear_history(user_uuid, "public")
    st.session_state["session_initialized"] = True
    # 不用 print + emoji：GBK 控制台（cp936）下 emoji 会抛 UnicodeEncodeError；
    # Streamlit 侧本来就该走统一日志通道（logger 内部对编码失败是安全的）。
    logger.info("streamlit_session_init", extra={"user": user["username"]})

st.title("📚 多租户 AI 智能问答系统")
st.write(f"当前用户：**{user['username']}** · 角色：`{user['role']}`")

# ================= 侧边栏 =================
with st.sidebar:
    st.header("👤 账户")
    if st.button("退出登录"):
        # 退出登录时，清理 session_state 并重新加载页面
        st.session_state.clear()
        st.rerun()

    if st.button("🧹 清空对话历史"):
        redis_service.clear_history(user_uuid, f"private:{user_uuid}")
        if is_admin:
            redis_service.clear_history(user_uuid, "public")
        st.success("✅ 对话历史已清空")

    # ---- 修改密码 ----
    # 补上审计里"提示用户改默认密码、但应用根本没有改密码入口"的缺口。
    # 逻辑全在 auth.change_password（不含任何 st.*），迁移到 FastAPI 后原样复用。
    with st.expander("🔑 修改密码", expanded=False):
        with st.form("change_password_form", clear_on_submit=True):
            pwd_old = st.text_input("当前密码", type="password", key="pwd_old")
            pwd_new = st.text_input("新密码（≥8 位）", type="password", key="pwd_new")
            pwd_new_2 = st.text_input("确认新密码", type="password", key="pwd_new_2")
            pwd_submit = st.form_submit_button("确认修改")
        if pwd_submit:
            pwd_ok, pwd_msg = change_password(user_uuid, pwd_old, pwd_new, pwd_new_2)
            if pwd_ok:
                st.success("✅ 密码修改成功！下次登录请使用新密码（当前会话继续有效）。")
            else:
                st.error(f"❌ {pwd_msg}")

    st.session_state["developer_mode"] = st.checkbox(
        "🔧 开发者模式（显示检索调试面板）",
        value=st.session_state.get("developer_mode", False)
    )
    show_debug_panel = is_admin or st.session_state["developer_mode"]

    # ---- 依赖服务自检：把"沉默的降级"明确暴露给管理员 ----
    if not ES_READY:
        st.error("❌ 无法连接 Elasticsearch，关键词检索不可用。")
        if is_admin:
            st.caption(f"详情：{ES_ERROR}")
    elif show_debug_panel:
        try:
            if not es_service.index_uses_ik():
                st.warning(
                    "⚠️ ES 索引使用的是 standard 分词器（未装 analysis-ik），"
                    "中文会被切成单字，BM25 精度受限。\n\n"
                    "修复：用 `es/Dockerfile` 构建带 IK 的 ES 镜像，"
                    "然后执行 "
                    "`python scripts/reset_knowledge_base.py --scope public --yes` "
                    "并重新上传文档。"
                )
        except Exception:
            pass

    st.header("📤 上传文档")
    if is_admin:
        target_db = st.radio("上传到哪个库？", ["私人库", "公共库"])
    else:
        target_db = "私人库"
        st.info("🔒 普通用户只能上传到私人库")

    # 用于刷新上传组件的 key
    if "uploader_key" not in st.session_state:
        st.session_state.uploader_key = 0

    # 决策：**暂不支持图片**。``type`` 就是浏览器里那个 ``accept`` 的来源——把 png/jpg/jpeg
    # 拿掉，用户在选择框里根本挑不到图（streamlit 服务端也会按同一份名单再滤一次）。
    # 真正兜底的是 API 侧（``api/routers/files.py:_reject_image_upload`` → 400
    # "图片格式暂不支持上传"）：界面上的限制永远只是"别让用户白点一次"。
    uploaded_file = st.file_uploader(
        "上传文档（支持 PDF/Word/PPT/Excel/HTML；暂不支持图片）",
        type=["pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "html", "md"],
        key=f"uploader_{st.session_state.uploader_key}",
    )

    if uploaded_file:
        if uploaded_file.size > 20 * 1024 * 1024:
            st.error("⚠️ 文件大小不能超过 20MB，请压缩后再上传。")
            st.stop()

        file_bytes = uploaded_file.getvalue()
        file_hash = hashlib.md5(file_bytes).hexdigest()

        if target_db == "私人库":
            scope = f"private:{user_uuid}"
        else:
            scope = "public"

        existing = file_state.check_duplicate(file_hash, scope)
        if existing:
            st.info(f"💡 文件 {uploaded_file.name} 已入库，无需重复上传。")
        else:
            file_uuid = new_uuid()
            source = build_source(user_uuid, file_uuid, uploaded_file.name)
            record_id = file_state.create_pending(
                file_hash, scope, uploaded_file.name, source, user_uuid, file_uuid
            )
            st.info(f"⏳ 状态：pending，记录 ID: {record_id}，开始解析...")

            try:
                with st.spinner("正在解析并生成父子块..."):
                    chunk_dict = pdf_service.process_uploaded_document_with_parent_child(
                        uploaded_file, user_uuid, file_uuid, source
                    )
                    parents = chunk_dict["parents"]
                    children = chunk_dict["children"]

                    if not parents or not children:
                        raise ValueError("Docling 未能从该文档中提取到任何有效文本。")

                    vectorstore = vector_service.get_vectorstore(scope)
                    vector_service.add_documents(vectorstore, children)

                    for p in parents:
                        es_service.index_parent_chunk(
                            p.metadata["parent_id"], p.page_content,
                            {"scope": scope, "page": p.metadata.get("page", 0),
                             "header": p.metadata.get("Header 1", ""),
                             "file_uuid": file_uuid, "source": source}
                        )
                    for c in children:
                        child_id = str(uuid.uuid4())
                        es_service.index_child_chunk(
                            child_id, c.metadata["parent_id"], c.page_content,
                            {"scope": scope, "page": c.metadata.get("page", 0),
                             "header": c.metadata.get("Header 1", ""),
                             "file_uuid": file_uuid, "source": source}
                        )

                file_state.mark_success(file_uuid, scope)
                # 强制刷新 ES：否则"刚上传完立刻提问"可能搜不到新块
                es_service.refresh_index()
                # 递增 key，让上传组件在下一次 rerun 时自动清空，
                # 避免文件一直挂在上传框里反复触发"已入库"提示
                st.session_state.uploader_key += 1
                st.success(f"✅ 已成功添加到 {target_db}！状态：success")

            except Exception as e:
                logger.error("upload_failed", extra={"err": str(e), "file_uuid": file_uuid})
                file_state.mark_failed(file_uuid, scope)
                try:
                    vectorstore = vector_service.get_vectorstore(scope)
                    vector_service.delete_by_source(vectorstore, source)
                    es_service.delete_by_file_uuid(file_uuid, scope)
                except Exception as cleanup_err:
                    logger.warning("cleanup_failed", extra={"err": str(cleanup_err)})
                st.error(f"❌ 入库失败：{e}")

    st.header("🗑️ 删除文件")
    if target_db == "私人库":
        scope = f"private:{user_uuid}"
    else:
        scope = "public"

    success_files = file_state.list_success_files(scope)
    if not success_files:
        st.info("📭 暂无可删除的文件")
    else:
        file_options = {f["file_name"]: f for f in success_files}
        chosen_name = st.selectbox("选择要删除的文件：", list(file_options.keys()))
        target_file = file_options[chosen_name]
        confirm = st.checkbox("⚠️ 确认删除？（不可恢复）", key=f"del_{target_file['file_uuid']}")
        if st.button("🗑️ 执行删除", disabled=not confirm):
            with st.spinner("正在删除..."):
                vectorstore = vector_service.get_vectorstore(scope)
                vector_service.delete_by_source(vectorstore, target_file['source'])
                file_state.delete_record(target_file["file_uuid"], scope)
                es_service.delete_by_file_uuid(target_file["file_uuid"], scope)
                st.success(f"✅ 已删除 `{target_file['file_name']}`")
                st.session_state.uploader_key += 1  # 刷新上传组件
                st.rerun()

    st.header("🔍 检索设置")
    search_scope = st.radio("搜索哪个库？", ["公共库", "私人库"])
    enable_rerank = st.checkbox("启用 Rerank 重排", value=True)

    if enable_rerank:
        rerank_model = st.selectbox(
            "选择重排模型：",
            ("轻量级 (MiniLM-L6，秒下秒跑)", "高精度 (BGE-base，约1GB，精度最高)")
        )
    else:
        rerank_model = None

    st.header("📄 文件级过滤")
    if search_scope == "公共库":
        current_scope = "public"
    else:
        current_scope = f"private:{user_uuid}"

    current_files = file_state.list_success_files(current_scope)
    file_filter_options = {"【全部文件】": None}
    for f in current_files:
        file_filter_options[f["file_name"]] = f["file_uuid"]

    def _on_file_change():
        redis_service.clear_history(user_uuid, current_scope)
        logger.info("streamlit_scope_switch_cleared_history", extra={"scope": current_scope})

    selected_file_name = st.selectbox(
        "限定检索范围（可选）：",
        list(file_filter_options.keys()),
        on_change=_on_file_change
    )
    selected_file_uuid = file_filter_options[selected_file_name]


# ================= 主区：问答 =================
question = st.text_input("请输入你的问题：", "张嘉亮的专业技能是什么？")

if st.button("🚀 提交问题") and question:
    with st.spinner("AI 正在思考..."):
        if search_scope == "公共库":
            scope = "public"
        else:
            scope = f"private:{user_uuid}"

        vs = vector_service.get_vectorstore(scope)
        all_docs = vs.get()

        if not all_docs.get('documents'):
            st.warning("⚠️ 该库还没有文档，请先上传 PDF。")
        else:
            history = redis_service.get_history(user_uuid, scope)
            rewritten_question = query_rewrite_service.rewrite_query(question, history)
            if rewritten_question != question:
                st.info(f"🔄 已重写为：**{rewritten_question}**")

            cache_data = redis_service.get_cached_answer(
                scope, rewritten_question, filter_key=selected_file_uuid or "all"
            )

            if cache_data:
                st.info("⚡ 命中 Redis 语义缓存，跳过检索与大模型调用！")
                answer = cache_data["answer"]
                citations = cache_data.get("citations", [])
                debug_info = cache_data.get("debug_info", [])
                st.success(answer)
                redis_service.append_history(user_uuid, scope, question, answer)
            else:
                if enable_rerank:
                    model_name = "BAAI/bge-reranker-base" if "高精度" in rerank_model else "cross-encoder/ms-marco-MiniLM-L-6-v2"
                else:
                    model_name = None

                retrieved_docs = retrieve_service.get_hybrid_retriever_with_es(
                    query=rewritten_question, scope=scope, vectorstore=vs,
                    k=10, enable_rerank=enable_rerank,
                    model_name=model_name if enable_rerank else "cross-encoder/ms-marco-MiniLM-L-6-v2",
                    file_uuid_filter=selected_file_uuid
                )

                THRESHOLD = 0.5
                max_score = max((d.metadata.get("rerank_score", 1.0) for d in retrieved_docs), default=0)
                logger.info(
                    "streamlit_retrieval_hit",
                    extra={"chunks": len(retrieved_docs), "max_score": round(max_score, 3)},
                )

                if not retrieved_docs or max_score < THRESHOLD:
                    answer = f"⚠️ 未在知识库中找到足够相关的信息（最高置信度 {max_score:.2f}，低于阈值 {THRESHOLD}）。请尝试换个问法，或补充相关资料。"
                    st.warning(answer)
                    redis_service.append_history(user_uuid, scope, question, answer)
                    citations, debug_info = [], []
                else:
                    answer_placeholder = st.empty()
                    full_answer = ""
                    for chunk in rag_service.generate_answer_stream(rewritten_question, retrieved_docs):
                        full_answer += chunk
                        answer_placeholder.markdown(full_answer + "▌")
                    answer_placeholder.markdown(full_answer)
                    answer = full_answer

                    refusal_keywords = [
                        "无法回答", "无法直接回答", "无法根据", "无法确定", "无法提供",
                        "无法得出", "无法判断", "无法直接得出",
                        "未提供", "未包含", "没有包含", "不包含", "没有涉及", "未涉及",
                        "很抱歉", "抱歉", "不知道", "不清楚"
                    ]
                    is_refusal = any(kw in answer for kw in refusal_keywords) or max_score < THRESHOLD

                    citations, debug_info = [], []
                    if is_refusal:
                        st.warning("⚠️ 未在知识库中找到足够相关的信息。")
                    else:
                        seen_refs = set()
                        for doc in retrieved_docs:
                            source = doc.metadata.get('source', '未知')
                            page = int(doc.metadata.get('page', 0)) + 1
                            display_name = source.split("__")[-1] if "__" in source else source
                            ref_key = (display_name, page)
                            if ref_key not in seen_refs:
                                seen_refs.add(ref_key)
                                citations.append({"source": display_name, "page": page})

                            content_len = len(doc.page_content)
                            is_parent = doc.metadata.get("parent_id") is not None
                            debug_info.append({
                                "length": content_len,
                                "is_parent": is_parent,
                                "preview": doc.page_content[:150],
                                "rerank_score": doc.metadata.get("rerank_score", 1.0)
                            })

                        redis_service.set_cached_answer(
                            scope, rewritten_question, answer, citations, debug_info,
                            filter_key=selected_file_uuid or "all",
                            # 与 FastAPI 入口写同一种 payload（两边共用同一个 Redis key）：
                            # 不存分数的话，这条答案在 Web 端命中缓存时就只能显示"无置信度"。
                            rerank_score=max_score,
                        )
                        st.toast("⚡ 答案已写入语义缓存", icon="✅")

                    redis_service.append_history(user_uuid, scope, question, answer)

            # ================= 渲染引用与调试面板 =================
            if citations:
                with st.expander("📎 引用来源"):
                    for idx, ref in enumerate(citations, 1):
                        st.markdown(f"**[{idx}]** `{ref['source']}` · 第 {ref['page']} 页")

            if show_debug_panel and debug_info:
                with st.expander("🛠️ 调试面板（验证RRF与父块回溯）"):
                    st.write("### 🎯 最终送入大模型的文档块详情")
                    for i, info in enumerate(debug_info, 1):
                        is_parent_str = "✅ 是（已回溯到完整父块）" if info["is_parent"] else "❌ 否（依然是碎子块）"
                        st.markdown(f"**第 {i} 块** | 长度: `{info['length']}` 字 | 是否父块: {is_parent_str} | 置信度: `{info['rerank_score']:.3f}`")
                        st.caption(f"内容预览: {info['preview']}...")

            # 🚨 关键：把本次结果保存到 session_state，供外部反馈区使用
            st.session_state["last_result"] = {
                "user_uuid": user_uuid,
                "scope": scope,
                "question": question,
                "rewritten_question": rewritten_question,
                "answer": answer,
                "citations": citations,
                "selected_file_uuid": selected_file_uuid,
                "timestamp": uuid.uuid4().hex[:8],
                "feedback_done": False,
            }


# ================= 用户反馈飞轮（独立于问答流程之外） =================
if "last_result" in st.session_state:
    res = st.session_state["last_result"]
    st.divider()

    if res.get("feedback_done"):
        st.success("✅ 感谢您的反馈，我们会持续优化！")
    else:
        st.write("**这个回答对您有帮助吗？**")
        col1, col2, col3 = st.columns([1, 1, 6])
        unique_id = res["timestamp"]

        with col1:
            if st.button("👍 有帮助", key=f"up_{unique_id}"):
                success = feedback_service.save_feedback(
                    res["user_uuid"], res["scope"], res["question"],
                    res["rewritten_question"], res["answer"], res["citations"],
                    rating="up", file_uuid_filter=res["selected_file_uuid"]
                )
                if success:
                    st.session_state["last_result"]["feedback_done"] = True
                    st.rerun()

        with col2:
            if st.button("👎 没帮助", key=f"down_{unique_id}"):
                st.session_state[f"show_reason_{unique_id}"] = True

        if st.session_state.get(f"show_reason_{unique_id}", False):
            reason = st.radio(
                "请选择原因：",
                ["资料过时", "回答错误", "答非所问", "其他"],
                key=f"reason_{unique_id}"
            )
            if st.button("提交反馈", key=f"submit_{unique_id}"):
                success = feedback_service.save_feedback(
                    res["user_uuid"], res["scope"], res["question"],
                    res["rewritten_question"], res["answer"], res["citations"],
                    rating="down", reason=reason, file_uuid_filter=res["selected_file_uuid"]
                )
                if success:
                    st.session_state["last_result"]["feedback_done"] = True
                    st.session_state[f"show_reason_{unique_id}"] = False
                    st.rerun()


# ================= 🧪 RAGAS 检索质量评测（仅管理员 / 开发者模式）=================
# 放在最后：它是"运营 + 面试演示"用的能力，不参与日常问答主流程。
if is_admin and show_debug_panel:
    st.divider()
    with st.expander("🧪 RAGAS 检索质量评测（黄金测试集）", expanded=False):
        st.caption(
            "对 evaluation/golden_set.json 里的标准问题跑一遍**完整线上链路**，"
            "再用 RAGAS 计算忠实度 / 答案相关性 / 上下文精确率 / 上下文召回率。"
            "会消耗 API 额度，演示时按需运行即可。"
        )

        col_a, col_b, col_c = st.columns([2, 2, 1])
        with col_a:
            eval_scope = st.selectbox(
                "评测范围",
                ["public", f"private:{user_uuid}"],
                key="ragas_scope",
            )
        with col_b:
            eval_limit = st.slider("题目数量", 1, 20, 5, key="ragas_limit")
        with col_c:
            st.write("")
            run_eval = st.button("🚀 开始评测", key="ragas_run")

        if run_eval:
            if not eval_service.ragas_available():
                st.warning(
                    "当前环境未装 RAGAS。评测依赖请装到独立的 .venv-eval（主环境刻意不装，"
                    "因为 ragas 会把 openai 3.x 降级成 1.x，影响生成链路的 SDK 版本）：\n\n"
                    "```bash\n"
                    "python -m venv .venv-eval\n"
                    ".venv-eval\\Scripts\\pip install -r requirements.txt -r requirements-eval.txt\n"
                    ".venv-eval\\Scripts\\python.exe scripts\\evaluate_ragas.py --scope public --limit 5\n"
                    "```\n\n"
                    f"（当前 ragas 版本：`{eval_service.ragas_version()}`）"
                )
            else:
                with st.spinner(f"正在评测前 {eval_limit} 题，大约 1-3 分钟..."):
                    try:
                        st.session_state["ragas_report"] = eval_service.run_ragas_evaluation(
                            scope=eval_scope, limit=eval_limit
                        )
                    except Exception as e:
                        logger.error("ragas_eval_failed", extra={"err": str(e)})
                        st.error(f"❌ 评测失败：{e}")

        report = st.session_state.get("ragas_report")
        if report:
            st.write(
                f"**总体指标** · scope=`{report['scope']}` · "
                f"样本 {report['sample_count']} 条 · "
                f"裁判 `{report['judge_model']}` · ragas `{report['ragas_version']}`"
            )
            st.dataframe(
                [
                    {"指标": report["metric_labels"].get(k, k), "得分": round(v, 4)}
                    for k, v in report["scores"].items()
                ],
                use_container_width=True,
                hide_index=True,
            )
            if report.get("rows"):
                st.write("**逐题明细**")
                st.dataframe(report["rows"], use_container_width=True)
            st.download_button(
                "⬇️ 下载 JSON 报告",
                data=json.dumps(report, ensure_ascii=False, indent=2),
                file_name="ragas_report.json",
                mime="application/json",
            )
            if report.get("report_path"):
                st.caption(f"报告已落盘：`{report['report_path']}`")
