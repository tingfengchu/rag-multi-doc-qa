📚 多租户 RAG 知识库问答平台

基于 Streamlit+LangChain+Chroma+智谱 GLM 构建的多租户 PDF 知识库问答系统。支持公共/私人库隔离、混合检索、Rerank 重排、防幻觉生成与一键Docker部署。

✨ 核心特性

- **多租户隔离**：基于 MySQL + 用户 UUID 实现数据隔离，公共库与私人库并存，杜绝越权访问。
- **数据一致性**：引入文件状态机（pending / success / failed）与补偿机制，杜绝上传失败导致的“幽灵数据”。
- **UUID 唯一化**：使用 `{owner_uuid}__{file_uuid}__{filename}` 作为文件唯一标识，彻底解决碰撞问题。
- **混合检索与重排**：BM25 + 向量检索进行混合召回，再交由本地 Cross-Encoder 模型进行精细重排，大幅提升检索精度。
- **防幻觉与溯源**：强约束 Prompt + `temperature=0` 抑制幻觉，强制要求回答附带引用编号，保证生成内容的可溯源性。
- **一键部署**：提供 `Dockerfile` 和 `docker-compose.yml`，支持一条命令拉起 MySQL 与 Streamlit 应用。

## 🛠️ 技术栈

- **前端**：Streamlit
- **后端与框架**：Python 3.12, LangChain
- **向量库**：ChromaDB
- **关系型数据库**：MySQL 8.0
- **Embedding & LLM**：智谱开放平台 (embedding-3, glm-4-flash)
- **重排模型**：HuggingFace Cross-Encoder (BAAI/bge-reranker-base 或轻量级 MiniLM)
