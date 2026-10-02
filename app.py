import os
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
import streamlit as st
import hashlib
from auth import require_login
from utils.ids import new_uuid, build_source
from utils.logger import logger
from services import file_state, pdf_service, vector_service, retrieve_service, rag_service
from langchain_core.documents import Document

st.set_page_config(page_title="多租户 RAG 助手", page_icon="📚")

user = require_login()
user_uuid = user["user_uuid"]
is_admin = user["role"] == "admin"

st.title("📚 多租户 AI 智能问答系统")
st.write(f"当前用户：**{user['username']}** · 角色：`{user['role']}`")

with st.sidebar:
    st.header("👤 账户")
    if st.button("退出登录"):
        st.session_state.pop("user", None)
        st.rerun()

    st.header("📤 上传文档")

    if is_admin:
        target_db = st.radio("上传到哪个库？", ["私人库", "公共库"])
    else:
        target_db = "私人库"
        st.info("🔒 普通用户只能上传到私人库")

    uploaded_file = st.file_uploader(
        "上传 PDF 文件（可多次上传）",
        type="pdf",
        key=f"uploader_{st.session_state.get('uploader_key', 0)}",
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
            st.info(f"💡 文件 {uploaded_file.name} 已入库，无需重复上传（全局去重生效）。")
        else:
            file_uuid = new_uuid()
            source = build_source(user_uuid, file_uuid, uploaded_file.name)

            record_id = file_state.create_pending(
                file_hash, scope, uploaded_file.name, source, user_uuid, file_uuid
            )
            st.info(f"⏳ 状态：pending，记录 ID: {record_id}，开始解析...")

            try:
                with st.spinner("正在解析并向量化..."):
                    new_splits = pdf_service.process_uploaded_pdf(
                        uploaded_file, user_uuid, file_uuid, source
                    )
                    if not new_splits:
                        raise ValueError("PDF 未解析出任何文本（可能是扫描件或纯图片）")
                    
                    vectorstore = vector_service.get_vectorstore(scope)
                    vector_service.add_documents(vectorstore, new_splits)

                file_state.mark_success(file_uuid, scope)
                st.success(f"✅ 已成功添加到 {target_db}！状态：success")

            except Exception as e:
                logger.error("upload_failed", extra={"err": str(e), "file_uuid": file_uuid})
                file_state.mark_failed(file_uuid, scope)
                try:
                    vectorstore = vector_service.get_vectorstore(scope)
                    vector_service.delete_by_source(vectorstore, source)
                except Exception as cleanup_err:
                    logger.warning("cleanup_failed", extra={"err": str(cleanup_err)})
                st.error(f"❌ 入库失败：{e}。已标记为 failed 并清理残留数据，请重试。")

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
                st.success(f"✅ 已删除 `{target_file['file_name']}`")
                st.session_state.uploader_key = st.session_state.get("uploader_key", 0) + 1
                st.rerun()

    st.header("🔍 检索设置")
    search_scope = st.radio("搜索哪个库？", ["公共库", "私人库"])
    enable_rerank = st.checkbox("启用 Rerank 重排", value=True)

    # 新增：选择重排模型
    if enable_rerank:
        rerank_model = st.selectbox(
            "选择重排模型：",
            (
                "轻量级 (MiniLM-L6，秒下秒跑)",
                "高精度 (BGE-base，约1GB，精度最高)"
            )
        )
    else:
        rerank_model = None  # 不启用 Rerank 时不需要模型

# ================= 主区：问答 =================
question = st.text_input("请输入你的问题：", "张嘉亮的专业技能里，关于RAG都有什么描述？")

if st.button("🚀 提交问题") and question:
    with st.spinner("AI 正在思考..."):
        # 1. 确定要查询的向量库
        if search_scope == "公共库":
            scope = "public"
        else:
            scope = f"private:{user_uuid}"

        vs = vector_service.get_vectorstore(scope)
        
        # 2. 从向量库拉取所有文档，构建 BM25 内存索引（生产环境建议持久化）
        all_docs = vs.get()
        splits = [Document(page_content=t, metadata=m) for t, m in zip(all_docs['documents'], all_docs['metadatas'])]

        if not splits:
            st.warning("⚠️ 该库还没有文档，请先上传 PDF。")
        else:
            # 3. 检索
            if enable_rerank:
                # 根据用户选择，映射到实际模型名称
                if "高精度" in rerank_model:
                    model_name = "BAAI/bge-reranker-base"
                else:
                    model_name = "cross-encoder/ms-marco-MiniLM-L-6-v2"
                
                retriever = retrieve_service.get_reranked_retriever(vs, splits, model_name=model_name)
            else:
                retriever = retrieve_service.get_hybrid_retriever(vs, splits)

            # ⚠️ 关键：这行必须跟 if/else 同级别，确保无论是否 Rerank 都会执行检索
            retrieved_docs = retriever.invoke(question)

            # 4. 生成回答
            answer = rag_service.generate_answer(question, retrieved_docs)
            
            st.success(answer)
            with st.expander("📎 引用来源"):
                for i, doc in enumerate(retrieved_docs, 1):
                    source = doc.metadata.get('source', '未知')
                    page = int(doc.metadata.get('page', 0)) + 1
                    st.markdown(f"**[{i}]** `{source}` · 第 {page} 页")