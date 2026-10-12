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
# 单元测试：纯离线，不依赖任何服务（32 个用例，约 1 秒）
.venv\Scripts\python.exe -m pytest tests/unit -v

# 集成测试：需要 MySQL / ES / Redis / Chroma 起好，且库里已有文档
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


### 📌 遗留提示

* `rag_knowledge` 旧索引仍在使用 `standard` 分词器。**装上 IK 后记得**：
  `python scripts/reset_knowledge_base.py --scope public --yes --drop-index`
  → 重启应用 → 重新上传文档。在此之前检索质量会低于预期，RAGAS 的
  `context_recall` 也会偏低。
* `scripts/unlock_files.cmd` 只在这台有透明加密的开发机上需要；换到新电脑（无加密）
  直接 `docker compose up -d --build` 即可。



