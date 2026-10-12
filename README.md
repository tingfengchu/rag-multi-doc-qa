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
- **关键词检索**：Elasticsearch 8.11 + IK 中文分词
- **评测**：RAGAS（faithfulness / answer_relevancy / context_precision / context_recall）

## 🚀 快速开始（本地开发）

```bash
# 1) 装依赖（Python 3.11+）
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2) 准备配置：复制示例并填入真实密钥
copy .env.example .env
copy rag_config.txt.example rag_config.txt

# 3) 起中间件（MySQL / ES / Redis）
docker compose up -d db es redis

# 4) 建一个管理员账号
.venv\Scripts\python.exe scripts\create_admin.py

# 5) 启动应用
.venv\Scripts\python.exe -m streamlit run app.py
```

浏览器打开 http://localhost:8501 ，用第 4 步创建的账号登录，上传文档后即可提问。

> 🔑 **登录后请先改掉默认密码**：左侧边栏 →「账户 → 🔑 修改密码」。
> `scripts/create_admin.py` 建的 admin 默认密码是 `ChangeMe_123!`，**务必替换**（以前应用里没有改密码入口，所以那个默认密码一直没被换掉）。

## 🐳 Docker 一键部署

```bash
copy .env.example .env       # 填好 MYSQL_ROOT_PASSWORD / MYSQL_PASSWORD / ZHIPU_API_KEY
docker compose up -d --build
# 浏览器访问 http://<服务器IP>:8501
```

部署要点：

| 事项 | 说明 |
| --- | --- |
| 密钥管理 | **所有密钥只放在 `.env` 里**（已被 `.gitignore` / `.dockerignore` 忽略）。`docker-compose.yml` 只写 `${VAR}` 占位符，启动时缺变量会直接报错，避免"用了默认弱密码还以为配好了"。 |
| 数据持久化 | 向量库统一放在 `./chroma_store`（容器内 `/app/chroma_store`，由 `CHROMA_BASE_DIR` 指定），只需挂一个卷就保住全部 scope 的数据。 |
| 模型缓存 | cross-encoder 权重缓存在 `hf_cache` 命名卷里，重建容器不必重新下载。 |
| 健康检查 | MySQL / ES / Redis 都带 healthcheck，`app` 会等它们 `service_healthy` 后再启动。 |

### ⚠️ Elasticsearch 中文分词（IK）——务必注意

官方 `elasticsearch:8.11.0` 镜像**不带** analysis-ik 插件，后果很直接：

* 建索引时会报 `failed to find analyzer [ik_max_word]`，索引根本建不出来；
* 即使绕开建索引，`standard` 分词器会把中文切成单字——
  「张嘉亮的专业技能」→ `张 / 嘉 / 亮 / 的 / 专 / 业 / 技 / 能`，
  专有名词基本失效，BM25 精度大幅下降。

本仓库的 `es/Dockerfile` 已经处理好了：基于官方镜像安装
`https://get.infini.cloud/elasticsearch/analysis-ik/<版本>`，并把
`ik_config/` 下的自定义词典（`custom.dic` / `stopword.dic`）COPY 进插件配置目录。
`docker-compose.yml` 的 `es` 服务默认就是 `build: ./es`。

* 网络受限拉不到插件包时，可临时把 `es` 服务改回官方 `image:`，
  代码会自动降级为 `standard` 分词器并在侧边栏打出警告（服务可用，但检索精度下降）；
* 装上 IK 之后，**必须重建索引并重新导入文档**才能生效：
  ```bash
  python scripts/reset_knowledge_base.py --scope public --yes --drop-index
  ```
  然后重启应用（自动按当前 IK 可用性重建索引）并重新上传文档。

## 🧪 RAGAS 自动评测

> **⚠️ 评测依赖必须装进独立的 `.venv-eval`，不能装进主 `.venv`。**
> 这不是洁癖，是两头实测都堵死了（细节也记在 `requirements-eval.txt` 顶部）：
>
> | 选择 | 结果 |
> | --- | --- |
> | `ragas` 0.4.x（新版） | `pip install --dry-run` → `instructor-1.3.2` → 要求 `openai<2`，把 `openai 3.29.0 → 1.109.1`、`langchain-openai 1.7.0 → 1.1.9`、`rich 15.0.0 → 13.9.4`，砸的正是生成链路的 SDK ❌ |
> | `ragas==0.2.15` / `0.3.0`（旧版） | 依赖层干净，但 `import ragas` 直接挂：`ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'`——`ragas<0.4` 在模块顶层硬 import 这个类，而 langchain-community 0.4.x 已把它删掉 ❌ |
>
> 所以：**主环境跑不了 ragas**，让评测环境自己解析一套自洽的版本组合（`openai 1.x` + `ragas 0.4.x`），与应用环境物理隔离。

```bash
# 必须两个文件一起装：requirements.txt 里故意没有 ragas / datasets
python -m venv .venv-eval
.venv-eval\Scripts\pip install -r requirements.txt -r requirements-eval.txt
```

跑评测（注意是 `.venv-eval` 里的 python）：

```bash
# 跑前 10 题
.venv-eval\Scripts\python.exe scripts\evaluate_ragas.py --scope public --limit 10

# 质量门禁：任一指标低于 0.7 就退出码 1（可接 CI）
.venv-eval\Scripts\python.exe scripts\evaluate_ragas.py --scope public --min-score 0.7

# 用自定义评测集（外部 JSON 也支持）
.venv-eval\Scripts\python.exe scripts\evaluate_ragas.py --golden-set path\to\my_set.json
```

主环境不带 ragas 也完全不影响应用：
`eval_service.ragas_available()` 返回 `False` 而不抛异常，
命令行脚本打印上面的安装指引后以退出码 2 结束（不再抛 traceback），
应用内评测面板给出同样的友好提示，Docker 镜像只装 `requirements.txt` 保持精简。
`services/eval_service.py` 对 ragas 0.1/0.2/0.4 三代 API 都做了兼容
（`EvaluationDataset` 优先、退回 `datasets.Dataset`；`evaluate()` 显式传 `llm/embeddings`），
将来 ragas 升到哪个版本都不用改代码。

四项指标（0~1，越高越好）：

| 指标 | 含义 |
| --- | --- |
| `faithfulness` | 答案是否完全由检索到的上下文支撑（不编造） |
| `answer_relevancy` | 答案是否切题 |
| `context_precision` | 检索结果里"有用"的部分排得够不够靠前 |
| `context_recall` | 标准答案所需信息有没有被全部捞回来 |

* **评测集**：`evaluation/golden_set.py`，23 道题，覆盖 public 库里的
  简历 / 自动驾驶面试资料 / SAGE 方案三份文档，`ground_truth` 都是从原文逐条核对的。
* **为什么评测集是 `.py` 而不是 `.json`**：开发机上的企业透明加密会把 `.json`
  写成 Python 读不了的密文（`%TSD-Header-###%`），`.py` 才能稳定读取。
  需要 JSON 时用 `--golden-set xxx.json` 传外部文件即可。
* **评测的就是线上链路**：`eval_service.collect_predictions()` 直接复用
  `retrieve_service` + `rag_service`，不是另写一个简化版去刷分。
  **只跑它、不跑裁判，也能定位问题**：它能直接打出每题的上下文条数与生成答案。
  实测第 1 题（问简历里的手机号/邮箱）只召回 3 段上下文、且都不含答案，
  生成端如实回答"文档中未提供"，而黄金集里参考答案是有的 —— 该题
  `context_recall` 必然为 0，典型的"索引里缺文档 / ES 未重建"信号。
* **报告落盘**：`evaluation_output/ragas_<scope>_<时间戳>.json`（已 gitignore），含总体指标与逐题明细。
* **应用内也能跑**：管理员（或开启开发者模式）登录后，页面底部有
  「🧪 RAGAS 检索质量评测」面板，可现场选题目数量、看指标表、下载 JSON 报告——面试演示很方便。

## ✅ 测试

```bash
# 单元测试：纯离线，不依赖任何服务（119 个用例，约 14 秒；bcrypt 故意慢是安全特性）
.venv\Scripts\python.exe -m pytest tests/unit -v

# 集成测试：需要 MySQL / ES / Redis / Chroma 起好，且库里已有文档
#  - test_auth_password.py / test_api_auth.py：改密码与 API 认证链路（临时账号自建自删）
#  - test_rag_integration.py：真实 LLM 端到端
.venv\Scripts\python.exe -m pytest -m integration -v

# RAGAS 端到端评测（真实调 LLM，默认被 pytest.ini 过滤掉）
.venv\Scripts\python.exe -m pytest -m ragas -v
```

其他实用脚本：

| 脚本 | 用途 |
| --- | --- |
| `scripts/create_admin.py` | 创建管理员账号 |
| `scripts/smoke_test_db.py` | 三张表的读写冒烟测试（**使用随机 scope，不会碰真实数据**） |
| `scripts/reset_knowledge_base.py` | 重置某个 scope 的 ES + Chroma + file_dedup（加 `--drop-index` 连索引一起删） |
| `scripts/evaluate_ragas.py` | RAGAS 四指标评测 |
| `scripts/evaluate_rag.py` | 轻量评测（不装 ragas 时的快速冒烟） |
| `scripts/unlock_files.cmd` | 仅在公司加密机器上需要：把源码转成明文供 `docker build` 读取 |

---

## 🔌 REST API（FastAPI，Phase 1）

Streamlit 版**继续可用**，API 与它**共用同一套业务层**（`services/` / `auth.py` / `utils/`），
所以两边行为一致，也不会出现"改了一边忘了另一边"。迁移分阶段推进，当前完成 Phase 1（后端接口）。

```bash
# ⚠️ --workers 1 是硬要求：每个 worker 都会各自加载 embedding / Cross-Encoder(~80MB) / Docling，
#    多进程 = 内存成倍增长 + 向量库连接重复（个人机、小服务器上只会更慢更抖）
.venv\Scripts\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1

# 交互式调试（OpenAPI 文档）
#   http://127.0.0.1:8000/api/docs
```

### 端点一览（16 个业务端点 + 2 个基础端点）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | 自助注册（**角色固定 user**，忽略 body 里的 `role`） | 公开 |
| POST | `/api/auth/login` | 登录，下发 HttpOnly 会话 Cookie | 公开 |
| POST | `/api/auth/logout` | 登出（服务端吊销会话，重复登出也返回 200） | 公开 |
| GET | `/api/auth/me` | 当前登录用户 | 登录 |
| POST | `/api/auth/password` | 改密码（成功后**吊销该用户全部会话**） | 登录 |
| GET | `/api/files?scope=` | 列出已入库文件 | 登录 + scope |
| POST | `/api/files/upload` | 上传并同步入库（multipart，≥20MB 拒绝、MD5 去重、失败自动回滚） | 登录 + scope |
| DELETE | `/api/files/{file_uuid}?scope=` | 删除文件（ES → 向量 → 状态记录） | 登录 + scope |
| POST | `/api/chat/stream` | **SSE** 流式问答 | 登录 + scope |
| GET | `/api/history?scope=` | 读取对话历史 | 登录 + scope |
| DELETE | `/api/history?scope=` | 清空对话历史 | 登录 + scope |
| POST | `/api/feedback` | 提交 👍/👎 反馈 | 登录 |
| GET | `/api/feedback/bad-cases` | 最近的差评样本 | admin |
| GET | `/api/admin/health` | ES / MySQL / Redis / 向量块数自检 | admin |
| POST | `/api/admin/eval/ragas` | RAGAS 评测（消耗 LLM 额度） | admin |
| GET | `/api/admin/eval/report?path=` | 下载报告（限定 `evaluation_output/`） | admin |
| GET | `/api/health` | 存活探针（不碰任何依赖，避免探针压垮服务） | 公开 |
| GET | `/api/docs` | OpenAPI 交互文档 | 公开 |

### 会话与安全设计

* **HttpOnly Cookie + Redis 不透明 token**，刻意不用 JWT：
  JWT 无法吊销，而"改密码后旧 token 仍然有效"会让改密码功能形同虚设；
  存 Redis 可以**即时吊销**（改密码 → 吊销该用户全部会话；登出 → 吊销当前会话）。
  `SameSite=Lax` 挡跨站 POST；生产 HTTPS 记得把 `SESSION_COOKIE_SECURE=true`。
* **scope 授权集中在 `api/deps.resolve_scope`**：`public` 读=登录即可、写=仅 admin；
  `private:<uuid>` 只允许本人（**admin 也不能读他人私人库**）；其它一律 400。
  这条以前靠 Streamlit 的 radio 天然成立，REST 化后必须显式校验，否则
  `?scope=private:<别人的 uuid>` 就是一次**不会报错的静默越权**（已用授权矩阵单测钉死）。
* `rerank_model` 用 `Literal` 白名单：否则客户端能指定任意 HF 仓库名让我们去下载。
* 报告下载做了路径穿越防护（只允许 `evaluation_output/` 下的文件）。
* 注册接口**忽略** body 里的 `role`：`create_user` 本身支持 role（脚本建管理员要用），
  所以"注册只能是普通用户"必须在路由层显式约束，否则谁都能把自己注册成管理员。

### SSE 事件契约（`POST /api/chat/stream`）

| event | data | 时机 |
| --- | --- | --- |
| `meta` | `{scope, question, rewritten_question, rewritten, cached?, refused?}` | 开始 / 命中缓存 / 判定拒答 |
| `token` | `{text}` | 答案增量（每个字/词一帧） |
| `citations` | `{citations: [{source, page}]}` | 生成结束且非拒答 |
| `debug` | `{debug_info: [{length, is_parent, preview, rerank_score}]}` | 生成结束且非拒答 |
| `done` | `{question, rewritten_question, answer, citations, scope, cached, max_score, refused}` | 结束 |
| `error` | `{message}` | 流中途出错（状态码此时已发出，只能靠事件报错） |

多行答案会被 JSON 转义进**同一帧**——SSE 靠换行分帧，直接拼正文会把一帧拆散，
前端拼接就错乱。nginx 反代需保留 `X-Accel-Buffering: no`（代码里已带），
否则"流式"会退化成"转圈很久然后一次吐完"。

### 迁移进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| Phase 1 | FastAPI 后端（本文档）+ Streamlit 版并存 | ✅ 已完成并验证 |
| Phase 2 | React 前端 + nginx 同源反代（同源后 Cookie 不再需要 CORS） | 待开始 |
| Phase 3 | 异步上传任务表、审计字段补全、多 worker 部署评估 | 待开始 |

> Phase 1 刻意保持**上传同步**（Docling 解析 10~60s）：异步化需要任务表 + 状态查询，
> 留到 Phase 3，先不把"重试 / 断点续传"这类复杂度提前引入。
> 另外两个 `st.cache_resource` 调用点（`retrieve_service` / `pdf_service`）已换成
> `utils.caching.resource_cache`：Streamlit 下走 `st.cache_resource`（行为不变），
> uvicorn 下走 `lru_cache`——否则模型会被**每个请求重新加载一遍**，接口能通但慢到不可用。

---

## 🔧 代码审计与修复清单

对全仓库做了一轮审计，下面是**已确认并修掉**的问题（每条都是先复现、再修，不是纸面判断）。

### 🔴 会导致数据损坏 / 安全泄露（高优先级）

1. **`scripts/smoke_test_db.py` 会删光真实数据**
   收尾清理写的是 `DELETE FROM file_dedup WHERE scope = 'public'`，跑一次冒烟测试就把
   公共库所有文件记录删干净（向量库和 ES 里数据还在，于是前端"文件管理"空白、
   重复上传检测失效）。**本仓库线上状态就是这个 bug 造成的**：Chroma 里 170 个块、
   `file_dedup` 却是 0 行。
   → 改用随机 scope `smoke:<8位hex>`，并把删除条件收紧成 `scope AND file_uuid` 双重限定。

2. **密钥硬编码进仓库**
   `docker-compose.yml` 明文写着 MySQL root 密码和真实的智谱 API Key（`rag_config.txt`
   同样如此）。一旦推到公开仓库，API Key 会被盗刷。
   → `docker-compose.yml` 全部改成 `${VAR}` 占位符（缺失时 compose 直接报错），
   `.env` / `rag_config.txt` 加进 `.gitignore` + `.dockerignore`，新增 `.env.example` /
   `rag_config.txt.example`。

### 🟠 功能不符合预期

3. **中文检索实际是"单字检索"**（最影响效果的一条）
   线上 `rag_knowledge` 索引的 mapping 里 `content` 只是 `{'type': 'text'}`，
   实测 `ik_max_word` 分词直接 400：`failed to find analyzer [ik_max_word]`；
   `standard` 分词器把「张嘉亮的专业技能」切成 `张/嘉/亮/的/专/业/技/能`，
   BM25 对专有名词几乎失效。索引是早期用 **ES 8.5.3** 建的，`init_es_index()`
   的 IK mapping 从来没真正生效；`ik_config/` 也没被挂进任何容器。
   → 新增 `es/Dockerfile`（官方镜像 + analysis-ik 插件 + 自定义词典），
   `docker-compose.yml` 的 `es` 改为 `build: ./es`；
   `es_service.init_es_index()` 先探测 IK，可用就用 IK mapping、不可用则降级为
   `standard` 并打 WARNING；侧边栏对管理员显示"当前索引没用 IK"的修复指引。

4. **引用页码永远是"第 1 页"**
   `_process_with_docling()` 把 `metadata["page"]` 硬编码为 `0`（线上 170 个块全是 0），
   而前端展示的是 `page + 1`，所以任何引用都显示第 1 页。
   → 改为按页导出（`export_to_markdown(page_no=n)`）再切分，存 0 基真实页码；
   取不到页码时自动回退到整篇导出，不会阻塞入库。

5. **用户私人库向量数据会随容器删除而丢失**
   `docker-compose.yml` 只挂了 `./chroma_public`，而私人库写的是
   `chroma_private_<uuid>`。
   → 新增 `CHROMA_BASE_DIR` 配置（默认仍是项目根目录，本地行为不变），
   compose 里统一挂 `./chroma_store`，所有 scope 的子库都在它下面。

6. **ES 没起来时整个应用白屏**
   `app.py` 在 import 阶段裸调 `es_service.init_es_index()`，任何 ES 异常都会让
   Streamlit 直接抛一大片 traceback，用户不知道是哪个依赖坏了。
   → 包 try/except，侧边栏给出明确错误提示，并把 IK 降级状态一并暴露给管理员。

7. **`build_source()` 对连续下划线不幂等**
   `filename.replace("__", "_")` 是非重叠单次替换，`____.pdf` 只会变成 `__.pdf`，
   于是 `source.split("__")` 切出 4 段，前端还原文件名错位。
   → 改用正则 `re.sub(r"_{2,}", "_", filename)`，并补了参数化单测锁死这个契约。

8. **上传组件不会自动清空**
   上传成功后文件仍留在 uploader 里，下次交互反复提示"已入库，无需重复上传"。
   → 成功后递增 `uploader_key` 自动重置组件，并调用 `es.refresh()`，
   修掉"刚上传完立刻提问却搜不到"的假象。

9. **`.xls` 上传会抛难懂的 ImportError**
   `pdf_service` 直接用 `pandas`，而 `pandas` / `openpyxl` / `xlrd` 从未写进
   `requirements.txt`（只是 docling 的间接依赖，等于"能跑但没声明"）。
   → 显式声明三个依赖，并在缺依赖时给出可读的报错。

10. **删除文件会留下永远清不掉的孤儿向量**
    `vector_service.delete_by_source()` 失败时只 `print` 不抛异常，调用方
    接着就删掉 DB 记录和 ES 文档，Chroma 里的向量从此没人能清。
    → 失败即抛异常；并改用公开 API（`get(where=...)` + `delete(ids=...)`）而不是
    依赖私有 `_collection`。ES 侧 `delete_by_file_uuid` 补上 `refresh=True`
    （否则刚删完还能搜到）与 `conflicts="proceed"`。

11. **`delete_by_query` / `search` 用的是已废弃的 `body=` 写法**
    elasticsearch-py 8.x 会告警、9.x 会移除。
    → 改成显式关键字参数（`query=` / `size=` / `refresh=`）。

### 🟡 工程与整洁度

12. **5 个包初始化文件拼错了**：`utils\_init_.py`、`services\_init_.py`、
    `tests\_init_.py`、`tests\integration\_init_.py`、`tests\unit\_init_.py`
    （单下划线）。命名空间包让 import 侥幸能跑，但打包/冻结会出问题。
    → 全部重命名为 `__init__.py`。

13. **`tests\unit\test_ids.py` 是 0 字节**，`utils/ids.py` 的函数完全没有测试。
    → 补齐 24 个用例（UUID 合法性、唯一性、文件名还原契约、边界文件名等）。
    **这批测试当场就抓出了上面第 7 条真实 bug。**

14. **`utils/logger.py` 的 JSON 日志混入 Python 内部字段**：3.12 起每个 LogRecord 都带
    `taskName`（3.13 还有 `otel*`），没过滤就会每条日志都多出 `"taskName": null`。
    → 扩充 `RESERVED_KEYS`。

15. **`retrieve_service` 里 6 个 import 是死代码**（`jieba`、`BM25Retriever`、
    `EnsembleRetriever`、`ContextualCompressionRetriever`、`CrossEncoderReranker`），
    拖慢启动。
    → 清理。

16. **`pytest.ini` / `Dockerfile` / `.dockerignore` / `ik_config\*` 是密文**，`docker build` 直接
    构建失败。→ 已重写为明文（实测确认文件头不再是 `%TSD-Header-###%`）。

17. **临时/垃圾文件清理**：删除 `COPY`（0 字节误创建的文件）、`_diag\`、`_plain_build\`、
    `_orphan_ibd_backup\`、各类 `_*.txt` 探针输出、`__pycache__`。

18. **`requirements.txt` 里的 `ragas` 会静默改写线上 LLM SDK 版本**（本轮最后才发现的一条）
    `requirements.txt` 末尾原本直接写着 `ragas` / `datasets`（不锁版本），
    意味着任何一次全新安装（包括 Docker 镜像构建）都会顺带改掉生成链路的 SDK：
    ```
    pip install --dry-run ragas
      -> Would install ... instructor-1.3.2 langchain-openai-1.1.9 openai-1.109.1 rich-13.9.4 ragas-0.4.3
    ```
    ragas ≥ 0.3.5 带进 `instructor`（要求 `openai<2`），把 `openai 3.29.0 → 1.109.1`、
    `langchain-openai 1.7.0 → 1.1.9`、`rich 15.0.0 → 13.9.4`。

    一开始想"锁个旧版本就行了"，实测被否掉：
    ```
    ragas==0.2.15 / 0.3.0  安装无冲突，但 import 直接失败：
      File "...\ragas\llms\base.py", line 8
        from langchain_community.chat_models.vertexai import ChatVertexAI
      ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'
    ```
    （langchain-community 0.4.x 删掉了 VertexAI 模块，而 ragas<0.4 在顶层硬 import。）
    新版降级 SDK、旧版根本 import 不进来——**主环境两条路都走不通**。

    → 结论与修复：
    * `requirements.txt` **移除** ragas / datasets（它是运行时依赖清单，不该被评测工具牵着走），
      Docker 镜像随之保持精简、SDK 版本可预期；
    * 评测依赖统一收进 `requirements-eval.txt`，并写明必须装进独立的 `.venv-eval`
      （附上述两条实测证据）；
    * 未安装时三处提示全部改成可执行的安装指引：CLI 退出码 2、应用内面板、
      `eval_service.run_ragas_evaluation()` 的 `RuntimeError`；
    * `eval_service` 保留对 ragas 0.1/0.2/0.4 三代 API 的兼容层，将来升版本不用改代码。


### 🔴 复盘补充：检索正确性（第二轮审计，2026-10-12）

19. **精排用的是英文模型，把中文检索已经排对第 1 的正确答案挤出了 top-3**
    （Q1 答不出手机号/邮箱、RAGAS `context_recall=0` 的真凶）

    `retrieve_service` 默认 `model_name="cross-encoder/ms-marco-MiniLM-L-6-v2"`，
    `app.py` 的「检索设置」默认勾选 Rerank 且默认选「轻量级 (MiniLM)」，
    `eval_service.collect_predictions()` 也是走**默认参数**——也就是说
    RAGAS 评的就是这条链路（不是评测集的问题）。

    实测（Q1「张嘉亮的手机号和邮箱分别是多少？」，知识库已按 IK 重建）：

    ```
    BM25  top10 : [1] score=16.75  '## 张嘉亮  13783395503 | zjl20041116@sjtu.edu.cn ...'
                  [2] score=14.10  （同一父块的另一个子块）
                  [3..10] 2.27 ~ 5.91          ← IK 生效后区分度极强
    Chroma top10: 联系方式块排第 8
    RRF         : 联系方式块 = 1/61 + 1/68 ≈ 0.031，是所有候选里最高的 → 本该排第 1

    enable_rerank=True  （英文 MiniLM）→ 最终 3 条 context 里**没有**联系方式块
        rerank_score = 0.999823 / 0.999177 / 0.999146（极差 0.0007，完全饱和）
        has_phone=False has_email=False → 模型只能答"文档中未提供该信息"
    enable_rerank=False （RRF 顺序）  → 第 1 条就是联系方式块
        has_phone=True  has_email=True ✅
    ```

    根因：MS MARCO 的 MiniLM 是**英文** Cross-Encoder，给中文 pair 打分时
    sigmoid 后全部堆在 0.999x，极差只有 0.0007 —— 所谓"精排"等价于随机打乱，
    于是把 BM25 第 1、RRF 也是第 1 的正确答案打掉了。

    → 修复：新增 `utils/rerank.py`，把"精排分数有没有区分度"抽成**可离线单测的纯函数**
    （`is_informative()` / `normalize_scores()` / `RERANK_MIN_SPREAD=0.02`）；
    `retrieve_service` 在重排前先过闸门：**没有区分度就保留 RRF 顺序**，
    并打 `rerank_no_discrimination` WARNING + 提示改用中文精排模型，
    绝不静默让饱和分数覆盖已经正确的排序。
    补 12 个单测锁死该契约（`tests/unit/test_rerank.py`，合计 44 个用例）。
    其中 `test_window_ignores_low_scoring_tail` 是**第一版修复漏判**的回归用例：
    一开始只看全局极差，而 top-10 里混着明显无关的低分文档、整体极差很大，
    于是判成"精排有效"、修复静默失效（实测结果与修复前逐位相同）。
    改成只看"决定输出的前 `TOP_N_FOR_CONTEXT`=3 条"的极差才真正生效。

    行为说明：中文语料在前端选「高精度（BGE-base）」才能获得真正的中文精排；
    继续用轻量级 MiniLM 时会自动退化为"不精排"，不会再劣化检索结果。

    修复后实测（同一条 Q1）：

    ```
    {"msg": "rerank_no_discrimination", "spread": 0.000677, "model": "...MiniLM-L-6-v2"}
    ⚠️ ... 已保留 RRF 顺序。中文语料建议改用 BAAI/bge-reranker-base。
    retrieved_docs=3   context_hit: 13783395503 / zjl20041116
      [1] '## 张嘉亮  13783395503 | zjl20041116@sjtu.edu.cn ...'
    A: 张嘉亮的手机号是13783395503，邮箱是zjl20041116@sjtu.edu.cn[1]。   ✅
    ```

20. **评测环境按文档的装法装出来是坏的：`import ragas` 直接崩（连踩 3 个坑）**

    这可能是本轮最有价值的一条：仓库从上一轮起就写着"RAGAS 评测脚手架"，
    但 `.venv-eval` 从未建起来过，**RAGAS 一次都没真跑过**，所以下面这个问题一直没暴露。

    * 坑 1：`requirements-eval.txt` 里只写 `ragas`（不锁下限）→ pip 装成 0.3.1，
      `import ragas` 崩。
    * 坑 2（**修正本清单上一版的结论**）：这不是 ragas 版本的问题。补上 `ragas>=0.4`
      后装到 0.4.3（镜像上的最新版），`import ragas` **照样崩** —— 因为 ragas 0.4.3 的
      `ragas/llms/base.py:12` 依然硬 import `ChatVertexAI`，而该模块在
      langchain-community ≥0.4 已被删除：

    ```text
    File "...\ragas\llms\base.py", line 12, in <module>
      from langchain_community.chat_models.vertexai import ChatVertexAI
    ModuleNotFoundError: No module named 'langchain_community.chat_models.vertexai'
    ```

      真正要钉的是 **`langchain-community<0.4`**（0.3.31 里仍有
      `chat_models/vertexai.py`，实测 `import ragas` → `0.4.3` 通过）。
    * 坑 3：只降 langchain-community 又会撞上 `langchain 1.x` ——
      `langchain_community.cross_encoders` 依赖 `langchain.retrievers`，
      而它在 langchain 1.x 已被删除：

    ```text
    services.retrieve_service: ModuleNotFoundError: No module named 'langchain.retrievers'
    ```

      → 评测环境必须**整体收敛到自洽的 0.3.x 栈**；另外 `requirements.txt` 带进来的
      1.x 遗留包要卸掉：`langchain-classic` 1.0.8 硬依赖 `langchain-core>=1.4.4`，
      与 core<1 在依赖层直接对撞（ResolutionImpossible）；`langgraph*` 同样要求
      core>=1.4.x（`pip check` 会报 3 条冲突），而 ragas 0.4.3 的依赖里**根本没有**
      langgraph。实测执行
      `pip uninstall -y langchain-classic langgraph langgraph-prebuilt langgraph-checkpoint langgraph-sdk`
      之后 `pip check` 干净：`No broken requirements found.`

    这些坑的共同表现：`eval_service.ragas_available()` 返回 False、
    `scripts/evaluate_ragas.py` 以退出码 2 结束（文档承诺的"装完就能评"不成立）。

    → 修复：`requirements-eval.txt` 写死 6 条 0.3.x 钉子 + 两步装法 + 自检命令。
    最终实测通过的组合（`.venv-eval`）：

    ```text
    ragas 0.4.3 / datasets 5.1.0
    langchain 0.3.30 / langchain-community 0.3.31 / langchain-core 0.3.86
    langchain-openai 0.3.35 / langchain-chroma 0.2.6 / langchain-text-splitters 0.3.11 / openai 1.109.1
    10 个 services.* 全部 import ok，ragas_available() = True，pip check 干净
    ```

    **这些降级只发生在 `.venv-eval` 里**：主环境仍是 langchain 1.4.4 +
    langchain-community 0.4.2 + openai 3.29.0，生成链路完全不受影响 ——
    这正是"评测依赖必须物理隔离"的实际价值。

21. **等权 RRF + 只取 top-3 上下文，把"只在 BM25 一路命中的正确答案"挤出了上下文**
    （RAGAS 前 5 题里第 4/5 题 `context_recall=0`、`answer_relevancy=0` 的直接原因）

    第 19 条修好精排后，RAGAS 第一次跑出四指标：faithfulness **0.9714** /
    context_precision **0.8000** / context_recall **0.6000**，但
    **answer_relevancy 0.4379 没过 0.50 门禁**。逐题明细显示前 3 题全对
    （0.84 / 0.86 / 0.49），第 4、5 题却是"文档中未提供该信息，无法回答"（0.0）。

    把这两题的三路召回拆开看：

    ```text
    Q5「实习项目里如何实现多路混合检索？权重怎么分配的？」
      BM25  [1] score=20.72  kw=[BM25, Chroma, 0.4, 0.6, 混合检索]  ← 答案块第 1 名
      Chroma top10：答案块根本不出现
      RRF   [4] rrf=0.01639  ← 被"两路都命中"的块（八股文 BM25#4 + 向量#2 = 0.03175）压到第 4

    Q4「在哪家公司实习、担任什么岗位、从什么时候开始？」
      BM25  [2] kw=[实习生, 2026.07]（答案块）  [6] '## 上海飒智智能科技有限公司'
      Chroma top10：一条简历块都没有（全是 SAGE / 自动驾驶噪声）
      RRF   [5] rrf=0.01613  ← 第 1~4 名全是两路都命中的块
    ```

    根因是**等权 RRF 的结构性特点**：只在一个臂命中、哪怕是该臂第 1 名的文档，
    `1/61 ≈ 0.0164` 必然小于"两个臂都出现"的文档 `1/61 + 1/62 ≈ 0.0325`。
    而当时只取 RRF 前 3 条当上下文（`final_docs[:3]`），这两块正好落在第 4/5 位，
    于是被整条丢掉 → 模型只能拒答 → `context_recall`/`answer_relevancy` 双双为 0。

    → 修复：`TOP_N_FOR_CONTEXT` 由 3 提到 **5**，并把上面这份实测依据写进代码注释；
    该常量同时也是精排区分度的判定窗口（`window=5`）。

    修复前后对比（同一条命令：`python scripts/evaluate_ragas.py --scope public --limit 5`，
    ragas 0.4.3 + glm-4-flash 当裁判，5 题）：

    | 指标 | 修复前（top-3） | 修复后（top-5） | 门禁 |
    | --- | --- | --- | --- |
    | faithfulness 忠实度 | 0.9714 | 0.8500 | ≥ 0.60 ✅ |
    | answer_relevancy 答案相关性 | **0.4379** ❌ | **0.7182** ✅ | ≥ 0.50 ✅ |
    | context_precision 上下文精确率 | 0.8000 | 0.7175 | — |
    | context_recall 上下文召回率 | 0.6000 | **0.8667** | — |

    代价与收益都符合预期：召回 +0.27、精确 −0.08（多两条上下文）、忠实度 −0.12
    （上下文变长，"没被上下文支撑的句子"更容易出现），但两项硬门禁全部通过。

    **仍存在的两个问题**（已记录，本轮未改）：
    * 第 4 题「在哪家公司实习」：岗位与时间答对，但**公司名被编造**成
      "AI大模型应用公司"（真值：上海飒智智能科技有限公司）。原因是公司名所在块
      RRF 排第 9、没进 top-5，而"实习岗位块"进去了，模型就用岗位名拼了个公司名 ——
      典型的"上下文部分相关时的局部幻觉"（faithfulness 仍判 1.0，因为这句话
      在字面上并不与上下文冲突）。
    * 第 5 题：检索到的上下文完全正确（`context_recall=1.0`），但模型在正确内容后
      自行补了一句"基于对两种检索方法的性能评估和需求分析"，faithfulness 掉到 0.25。

    这两条属于"生成侧 Prompt 约束 + 融合策略"的范畴（例如要求实体必须逐字来自上下文、
    或改成加权融合让公司名块进入上下文），需要更大评测集验证，列为后续项。

    遗留：等权融合在"中文 + IK BM25"场景下偏向"两路都出现的中等文档"。
    要进一步提升应改成加权融合或"BM25 主排序 + 向量补召回"，
    但那属于排序策略调优，需要更大评测集验证，本轮只做了有实测支撑、
    且不引入新回归的那一步（窗口 3 → 5）。


### 🔴 第二轮补充：账户安全（2026-10-12）

22. **一直提示"请尽快登录修改密码"，但应用里根本没有改密码入口 —— 默认密码从未被换过**

    `scripts/create_admin.py` 建号时打印"请尽快登录修改"，但 `app.py` 侧边栏只有
    「退出登录 / 清空对话历史」，**全仓库没有任何修改密码的代码路径**，
    所以 `users` 表里那个 admin 至今仍能用默认密码 `ChangeMe_123!` 登录
    （实测 bcrypt 校验通过，不是猜测）。

    修复（`auth.change_password` + 侧边栏表单）：

    * 新增 `auth.change_password(user_uuid, old, new, confirm)`，与 `create_user` 同为
      `(bool, 文案)` 约定、文案可直接展示；**必须提供旧密码**（否则会话被劫持
      就等价于账号被夺走），且新密码不能与旧密码相同（避免"以为改了其实没改"）；
    * 密码强度规则（≥8 位、≤72 字节）**抽成 `utils.passwords` 单独一份**，注册与改密码共用。
      规则散在两处必然漂移，"注册有校验、改密码没校验"是最典型的口令漏洞；
      bcrypt 只吃前 72 字节这件事也一并锁进单测（"拒绝"而不是"静默截断"）；
    * UI 在侧边栏「账户 → 🔑 修改密码」，`st.form(clear_on_submit=True)` 提交后自动清空输入框；
    * 被拒绝的路径**一律不写库**，并用集成测试逐条验证（否则会出现"提示失败但密码已经变了"）。

    验证：

    ```bash
    .venv\Scripts\python.exe -m pytest tests/unit -q          # 66 passed（44 + 新增 22）
    .venv\Scripts\python.exe -m pytest -m integration -v      # 含改密码 7 例，临时账号自建自删
    ```

    附带说明：`utils.passwords` 刻意不 import streamlit / MySQL，所以 FastAPI 迁移时
    `api/routers/auth.py` 可直接复用，不必反向依赖 `auth.py`；
    `hash_password` / `verify_password` 仍在 `auth.py` 转出，历史用法（含 `scripts/`）不受影响。


### 🔵 第三轮：FastAPI Phase 1（2026-10-12）

23. **REST 化会把"天然成立"的隔离变成"必须显式校验"** —— scope 授权收口

    Streamlit 版把 scope 藏在 `st.radio` 的两个选项里，用户没有输入通道，所以
    "只能访问 public / 自己的私人库"天生成立。换成 REST 后 `scope` 变成
    **客户端可任意构造的字符串**，一次漏判就是静默越权：
    `?scope=private:<别人的 user_uuid>` 能直接列他人私人库、读他人对话历史——
    不报错、不告警，只是把数据交出去。

    → 新增 `api/deps.resolve_scope` 作为**唯一**收口点（读/写权限分开：
    普通用户可读 public 但不可写；`private:<uuid>` 只允许本人，**admin 也不行**），
    配 20 例授权矩阵单测（含 `private:<自己>:<后缀>` 这类拼接后门）。

24. **改密码只换哈希、不吊销会话 = 只做了一半** —— 会话必须可吊销

    刚上线的改密码功能如果配 JWT，被窃取的旧 token 在过期前依然可用，
    等于"改了密码但攻击者还在屋里"。
    → 会话改用 **Redis 不透明 token + HttpOnly Cookie**（不用 JWT）：
    改密码吊销该用户全部会话、登出吊销当前会话；
    集成测试专门验证"手里揣着旧 Cookie 也必须 401"（证明是服务端吊销，不是删 Cookie）。

25. **两个 `st.cache_resource` 在非 Streamlit 运行时会退化成普通函数**

    `retrieve_service`（Cross-Encoder ~80MB）与 `pdf_service`（Docling 转换器）
    都用 `st.cache_resource` 做"只加载一次"。迁到 uvicorn 后该装饰器不再生效，
    变成**每个请求重建一次模型**——接口能通、日志正常，只是慢到不可用，
    属于最难发现的性能回归。
    → 迁移前先引入 `utils/caching.resource_cache`（Streamlit 下走
    `st.cache_resource`，其它运行时走 `lru_cache`），并加源码级回归测试
    （服务层禁止再出现裸 `@st.cache_resource`）。


### 📌 遗留提示

* ✅ **IK 重建 + 语料对齐已完成（2026-10-12）**：`rag_knowledge` 已 `--drop-index`
  重建，mapping 实际使用 `ik_max_word_analyzer`（`ik_available=True`、`index_uses_ik=True`）；
  文档重新入库后与 `evaluation/golden_set.py` 的语料口径对齐为 **3 份**
  （`1790569531500.pdf` 简历 / 自动驾驶算法与嵌入式具身智能运控 / SAGE 方案）——
  实测 **Chroma 89 块 / ES 178 条（89 父 + 89 子）/ file_dedup 3 条 success**。
  多灌的那份「我的简历 (AI应用开发)(3).pdf」是同一份简历的另一版（重复语料，
  实测两份都含 13783395503 / 实习经历），已按 app 自己的删除链路
  （Chroma → file_dedup → ES）清理掉，避免污染 context_precision。
* ✅ **RAGAS 已可运行且有基线分数**（2026-10-12，public 库 3 份文档，ragas 0.4.3 +
  glm-4-flash 裁判）：`--limit 5` → faithfulness **0.8500** / answer_relevancy
  **0.7182** / context_precision **0.7175** / context_recall **0.8667**，
  两项硬门禁（≥0.60 / ≥0.50）全部通过。报告落在 `evaluation_output/`（已 gitignore）。
  跑完整 23 题：`.venv-eval\Scripts\python.exe scripts\evaluate_ragas.py --scope public`
  与 `.venv-eval\Scripts\python.exe -m pytest -m ragas -v`。
* **两个已知残留问题**（详见第 21 条）：① 上下文只含"实习岗位块"时，模型会把公司名
  编成"AI大模型应用公司"（真值是上海飒智智能科技有限公司）；② 模型会在正确内容后
  补写"理由"句，拉低 faithfulness。根治方向是生成侧 Prompt 约束（实体必须逐字来自
  上下文）+ 加权融合（让公司名块进入上下文），需要更大评测集验证后再改。
* **账户安全（2026-10-12 已补一半）**：自助「修改密码」已上线（侧边栏），但**还没有**
  「管理员重置他人密码」和「忘记密码」流程；注册 tab 仍是自助开放（`auth.require_login`），
  对外演示前建议加邀请码或关闭。**admin 的默认密码 `ChangeMe_123!` 建议第一次登录就改掉**
  ——现在有入口了，别再拖。
* **账户基线（实测）**：`users` 表只有 1 个账号（`admin` / role=admin / is_active=1）；
  MySQL root 是 `Lm060205.` 且应用也用它连库（生产建议单独建最小权限账号）；
  Redis 无密码、ES 未开认证，但都只绑 `127.0.0.1`。`.env` / `rag_config.txt` 均被 gitignore，
  且 `git log --all -S` 实测这两个真实密钥**从未进过任何提交**，所以无需因仓库泄露而轮换。
* **中文语料建议把重排模型换成 `BAAI/bge-reranker-base`**：轻量级 MiniLM 是英文模型，
  对中文问句没有区分度（见第 19 条）。回退机制只会"兜住不劣化"，
  想要真正的精排收益必须换模型（前端「检索设置 → 选择重排模型 → 高精度」）。
* `scripts/unlock_files.cmd` 只在这台有透明加密的开发机上需要；换到新电脑（无加密）
  直接 `docker compose up -d --build` 即可。
* **Docker 演示的向量库要手动对齐**：本地开发（`.venv` 直接跑 `app.py`）用的是项目根
  目录的 `./chroma_public/`；容器里 `CHROMA_BASE_DIR=/app/chroma_store`、挂载的是
  `./chroma_store`，也就是容器读 `./chroma_store/chroma_public/`。
  本次已把本地库复制过去（`robocopy chroma_public chroma_store\chroma_public /E`，
  逐字节校验一致），所以容器启动后公共库不是空的。换机器或重新灌数据后记得再对齐一次
  —— ES 与 MySQL 都是共享容器（`es_data` / `mysql_data`），只有 Chroma 是文件级差异。
* 小提示：ragas 0.4.3 会对 `aevaluate()` 报 `DeprecationWarning`（应用侧兼容层调用的），
  不影响打分结果；将来升级 ragas 时按官方提示迁到 `@experiment` 即可。



