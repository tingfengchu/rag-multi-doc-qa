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
| 数据持久化 | 向量库统一放在 `./chroma_store`（容器内 `/app/chroma_store`，由 `CHROMA_BASE_DIR` 指定），**上传原件**放在 `./file_store`（`FILE_STORE_DIR`）——两个都要挂卷：丢掉前者检索会空、丢掉后者「下载原文」全 404。 |
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
# 单元测试：纯离线，不依赖任何服务（535 个用例，约 100 秒；其中约 60 秒是 endpoint 级
#  测试（test_upload_scope.py / test_file_download.py / test_file_library.py 等）一次性
#  导入 torch/chromadb 这类重依赖的费用；bcrypt 故意慢是安全特性）
.venv\Scripts\python.exe -m pytest tests/unit -v

# 集成测试：需要 MySQL / ES / Redis / Chroma 起好，且库里已有文档
#  - test_auth_password.py / test_api_auth.py：改密码与 API 认证链路（临时账号自建自删）
#  - test_rag_integration.py：真实 LLM 端到端
#  - test_cache_invalidation_e2e.py：删文件 → 引用它的语义缓存失效 → 再问同样的问题
#    必须重新检索（2026-10-14 事故：删了文件还命中缓存 → 点引用下载 404）。服务不可用
#    会跳过；临时账号 + 私人库自建自删，跑完不留账号 / 缓存 / 流水 / Chroma 目录
.venv\Scripts\python.exe -m pytest -m integration -v

# RAGAS 端到端评测（真实调 LLM，默认被 pytest.ini 过滤掉）
.venv\Scripts\python.exe -m pytest -m ragas -v

# 前端 E2E（Playwright，真前端 + 真后端；模块 2.1 / 2.2 / 4 / 5 / 5b / 6）
# 前提：① 后端 8000 已起；② 前端 5173 已起（npm run dev）；
#      ③ **必须**打开注册开关（用例自己建临时账号，不动任何真实账号）——没开整轮直接红：
#         rag_config.txt 里写 allow_register = true，或临时用环境变量启动后端：
#         cmd /c "set ALLOW_REGISTER=true && .venv\Scripts\python.exe -m uvicorn api.main:app --port 8000 --workers 1"
#      Playwright 的 globalSetup 另会建一个**可丢弃的管理员**（e2e_admin_file，走
#      scripts/create_admin.py）：自助注册只能建普通用户，而"管理员看到全部文件"这条
#      必须用管理员验；账号用 e2e_ 前缀，跑完由 globalTeardown 一起清掉。
cd rag-frontend
npx playwright install chromium   # 首次需要下载浏览器（约 200MB）
npx playwright test               # 后端没开注册开关会**直接失败**（见下方 ⚠️），不会静默跳过
```

> ⚠️ **跑 E2E 前必须用 `ALLOW_REGISTER=true` 启动后端**（或 `rag_config.txt` 里写
> `allow_register = true`），**否则整轮测试会变红，而不是静默跳过**：
> `e2e/global-setup.ts` 会先 `GET /api/auth/config`，`allow_register` 不是 `true`
> 就直接抛错、用例一条都不跑；`e2e/helpers.ts` 的 `registerTempUser` 同样从
> "返回 null 让调用方 skip"改成了抛错。
>
> 这道闸门是为了堵住"假绿"：后端默认配置就是 `allow_register=false`，而依赖临时账号的
> 用例（模块 5b / 6 的新用例全在内）以前会被 `test.skip` 静默跳过，整轮跑出来是
> `3 passed / 14 skipped` 且**退出码 0** —— 新功能一行都没验，CI 却是绿的。
> 改完之后：`npx playwright test` 绿 = 这些用例**真的跑过**。

| E2E 用例 | 覆盖 |
| --- | --- |
| `e2e/password-change.spec.ts` | 登录 → 改密码（含三类前端校验）→ 被踢回 `/login` → 旧密码失效、新密码可登录 |
| `e2e/register-gating.spec.ts` | 注册入口与后端开关一致；**关着时接口也 403**；打开时注册成功 → 用户名回填 → 能登录 |
| `e2e/scope-both.spec.ts` | 侧栏默认 `both`；回答上方出现「检索范围：…」；空私人库时如实显示**「公共库（我的私人库为空，已跳过）」**；空库提问给明确提示 |
| `e2e/rerank-default.spec.ts` | 「检索设置」里重排模型**默认选中 bge-reranker-base**，且 MiniLM 仍在下拉里、能切、能切回 |
| `e2e/citation-noise.spec.ts` | 问「张嘉亮是谁？」：**引用来源里没有无关文件**（自动驾驶算法.pdf 实测只有 0.00004 分），每条引用都带分数且 ≥ `CITATION_THRESHOLD`，**「最高置信度」不再是 1.00** |
| `e2e/rewrite-followup.spec.ts` | 先问「张嘉亮是谁？」再追问「**他的实习经历呢？**」：`已重写为：` 的文案里**必须含「张嘉亮」且不再含「他的」**（LLM 没消解时由历史实体兜底替换） |
| `e2e/file-management.spec.ts` | 模块 5：管理员看到公共库那 3 个文件（表头含库/版本/更新时间/上传人）并**下载成功**（校验落盘文件名与字节数）、**「预览」在新标签页里打开 inline 原件**（`blob:`，不落盘、接口回的是 PDF 字节）；普通用户只看到公共库文件；别人的私人库文件**列表里查不到**且直接按 uuid 下载 → 403；点引用标签 → 弹窗有摘录/置信度 → 下载原文成功、**「查看原文」打开的是带 `#page=N` 的预览 URL**；同名文件传**不同内容**两次 → 默认只列 **v2**，打开「显示历史版本」后 v2 **和** v1 都在（v1 带「历史版本」标签）；传**同一份文件**（MD5 相同）两次 → **被去重**（不新建版本，历史版本里也只有那一条）；**删除**（2026-10-13）：管理员上传临时文件 → 点「删除」→ 弹二次确认（「取消」什么都不会发生）→ 确认后行消失且再删一次 404；普通用户对公共库的行**没有删除入口**、绕过界面直接调接口也是 403 且文件原封不动；普通用户删**自己私人库**的文件成功 |
| `e2e/feedback-admin.spec.ts` | 模块 6：点 👎 → 按钮变「已反馈」→ 差评样本**真的落库**（管理员会话查 `bad-cases`）；「资料缺失/反馈」弹窗提交后同一条回答同步变「已反馈」；管理员 `/admin` 四块齐全且**刚问过的问题出现在高频提问里**（同时证明检索流水在写）；普通用户 `/admin` → 「需要管理员权限」且侧栏没有入口、接口 403 |

> 测试账号**自动清理**：用例自建的 `e2e_*` 账号（含 globalSetup 建的那个可丢弃管理员）
> 在 E2E 跑完后由 Playwright 的 `globalTeardown`
> （`e2e/global-teardown.ts` → 后端 `scripts/purge_e2e_users.py`）删掉；
> pytest 侧用的是**同一份**删除逻辑（`tests/conftest.py` 的 session 夹具）。
> 用例里**上传**的文件（`e2e/file-management.spec.ts` 的私人库小文件）则用产品自己的
> 删除接口收尾（ES 块 / 向量 / 记录 / 留存原件一起删），所以不会在库里留下垃圾。
> 手工保洁：`.venv\Scripts\python.exe scripts\purge_e2e_users.py --dry-run`（只统计不删）。

E2E 里踩到的 antd 坑（都写在用例注释里，省得下次再花半小时）：

* **两汉字的按钮会被 antd 插空格**（`登录` → `登 录`）→ `helpers.antdButton()` 用正则匹配；
* **Modal 关闭后仍在 DOM**（`getByLabel` 会同时命中登录表单和注册弹窗 → strict mode violation）
  → `helpers.loginForm()` 用 `#login` 限定；
* **单选 Select 不再渲染 `.ant-select-selection-item`**（antd v6）→ 断言控件文案而不是内部 class；
* **下拉是虚拟列表**（`rc-virtual-list`）：`getByRole('option')` 能定位，但 `toBeVisible()` 判 hidden
  → 用键盘 `ArrowDown/ArrowUp + Enter` 选择；`getByText` 还会同时命中外层/内层两个 div。

其他实用脚本：

| 脚本 | 用途 |
| --- | --- |
| `scripts/create_admin.py` | 创建管理员账号（`-u/--username`、`-p/--password` 可选；E2E 用 `e2e_admin_file` 建可丢弃管理员） |
| `scripts/purge_e2e_users.py` | 清理测试账号（只删 `e2e_` / `test_` 前缀；`--dry-run` 只统计）——E2E 与 pytest 的保洁共用这一份 |
| `scripts/smoke_test_db.py` | 三张表的读写冒烟测试（**使用随机 scope，不会碰真实数据**） |
| `scripts/apply_schema_updates.py` | **幂等**地补模块 5 / 6 的表结构改动（`version` / `is_archived` / `feedback.q_hash` / `retrieval_log`）；`--dry-run` 只看要改什么。MySQL 8 没有 `ADD COLUMN IF NOT EXISTS`，直接 `mysql < migrations/004*.sql` 重复执行会报 1060 |
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

### 端点一览（21 个业务端点 + 2 个基础端点）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| POST | `/api/auth/register` | 自助注册（**角色固定 user**，忽略 body 里的 `role`；受 `allow_register` 开关控制，**默认关闭**→403） | 公开 |
| GET | `/api/auth/config` | 登录页开关 `{allow_register}`（**免登录**：登录页自己要调） | 公开 |
| POST | `/api/auth/login` | 登录，下发 HttpOnly 会话 Cookie | 公开 |
| POST | `/api/auth/logout` | 登出（服务端吊销会话，重复登出也返回 200） | 公开 |
| GET | `/api/auth/me` | 当前登录用户 | 登录 |
| POST | `/api/auth/password` | 改密码（成功后**吊销该用户全部会话**） | 登录 |
| GET | `/api/files?scope=` | 列出已入库文件（**某一个具体库**；文件级过滤下拉用）。`include_archived`（模块 5，默认 false）控制是否连**历史版本**一起列 | 登录 + scope |
| GET | `/api/files/library` | 文件管理面板：公共库 + **我自己的**私人库，含上传人 / 版本 / 更新时间 / 原件是否还在 / **能否删除（`can_delete`）**——`can_delete` 由后端按会话算（就是 `resolve_scope(write=True)` 的预演），面板据此决定给不给「删除」按钮。`include_archived`（模块 5）对应面板的「显示历史版本」开关 | 登录（库范围由会话决定，客户端传什么都改不了） |
| GET | `/api/files/{file_uuid}/download` | 下载**原件**（`Content-Disposition` 带原始中文名）；历史版本同样可下 | 登录；`public` 人人可下，`private:<owner>` **仅本人**（他人含管理员 403，记录缺失 404） |
| GET | `/api/files/{file_uuid}/preview` | **在线预览**原件：`Content-Disposition: inline`（PDF 等就地打开，可配 URL 上的 `#page=N` 跳页）。HTML / SVG 等能被当页面渲染的类型**仍回 `attachment`**（blob URL 继承本站 origin，内嵌等于放 XSS），前端据此降级成下载 | 与下载**完全一致**（同一份实现 `_row_and_original`；`private:<owner>` 仅本人，403 文案是「无预览权限…」） |
| POST | `/api/files/upload` | 上传并同步入库（multipart，≥20MB 拒绝、**图片一律 400「图片格式暂不支持上传」**、失败自动回滚）。**同库同 MD5 = 去重**（200 + `duplicate=true`「已入库，无需重复上传」，不新建版本）；**同名 + 内容不同 = 新版本**（旧记录转历史版本，`version = 旧 + 1`，见模块 5） | 登录 + scope（**写公共库需 admin / authorized_user**） |
| DELETE | `/api/files/{file_uuid}?scope=` | 删除文件（**语义缓存 → ES 父子块 → Chroma 向量 → MySQL 记录 → 留存原件**，见 §3.2）；**历史版本也能删**；记录不在该库里 → 404 | 与上传同一套写权限：`public` 需 admin / authorized_user，`private:<自己>` 仅本人（否则 403；`both` → 400） |
| POST | `/api/chat/stream` | **SSE** 流式问答（`scope` 支持 **`both`** = 公共库 + 我的私人库） | 登录 + scope |
| GET | `/api/history?scope=` | 读取对话历史（`both` 可用） | 登录 + scope |
| DELETE | `/api/history?scope=` | 清空对话历史（`both` 可用） | 登录 + scope |
| POST | `/api/feedback` | 提交 👍/👎 反馈（同一个人对同一个问题只留一条：服务端按 `q_hash` 在 10 分钟窗口内判重） | 登录 |
| GET | `/api/feedback/bad-cases` | 最近的差评样本 | admin |
| GET | `/api/admin/stats` | **运营看板**（模块 6）：高频提问 / 被检索最多的文档 / 检索无结果的问题 / 反馈汇总。`window` 控制统计最近多少次检索（默认 5000，下限 50） | admin |
| GET | `/api/admin/health` | ES / MySQL / Redis / 向量块数自检 | admin |
| GET | `/api/admin/users` | 账号列表（授权面板用，**不含口令哈希**） | admin |
| PUT | `/api/admin/users/{user_uuid}/role` | 改他人角色（`user` / `authorized_user` / `admin`），改完**吊销其全部会话** | admin |
| POST | `/api/admin/eval/ragas` | RAGAS 评测（消耗 LLM 额度） | admin |
| GET | `/api/admin/eval/report?path=` | 下载报告（限定 `evaluation_output/`） | admin |
| GET | `/api/health` | 存活探针（不碰任何依赖，避免探针压垮服务） | 公开 |
| GET | `/api/docs` | OpenAPI 交互文档 | 公开 |

### 会话与安全设计

* **HttpOnly Cookie + Redis 不透明 token**，刻意不用 JWT：
  JWT 无法吊销，而"改密码后旧 token 仍然有效"会让改密码功能形同虚设；
  存 Redis 可以**即时吊销**（改密码 → 吊销该用户全部会话；登出 → 吊销当前会话）。
  `SameSite=Lax` 挡跨站 POST；生产 HTTPS 记得把 `SESSION_COOKIE_SECURE=true`。
* **scope 授权集中在 `api/deps.resolve_scope` / `resolve_scope_set`**：`public` 读=登录即可、
  写=**admin 或 authorized_user**（见下面的权限矩阵）；`private:<uuid>` 只允许本人
  （**admin 也不能读他人私人库**）；其它一律 400。
  这条以前靠 Streamlit 的 radio 天然成立，REST 化后必须显式校验，否则
  `?scope=private:<别人的 uuid>` 就是一次**不会报错的静默越权**（已用授权矩阵单测钉死）。
* **检索范围 `both`（模块 4）**：`POST /api/chat/stream` 与 `/api/history` 额外接受
  `scope="both"`，服务端解析成 `[public, private:<自己>]`（顺序固定），
  每个库各跑一遍 BM25+向量召回，合并后**全局精排一次**，每个块的
  `metadata.scope` 决定它在引用里标注「公共库 / 我的文档」。
  三个必须记住的点：
  1. 私人库部分永远用**会话里的 uuid** 重拼，客户端构造 `both` 不可能夹带别人的库
     （`resolve_scope_set` 的返回是白名单，不是字符串拼接结果）；
  2. `both` 是**只读**语义：上传 / 删除 / 列文件仍然只接受具体库（`both` → 400），
     否则"文件传到哪个库"没有答案；
  3. 缓存 / 历史 / `done` 事件共用同一个 key（`deps.scope_key`）：单库时就是原来的
     `public`、`private:<uuid>`（**老缓存与老会话不失效**），多库时是
     `public+private:<uuid>`（带 uuid，天然按用户隔离）。
* **角色模型（模块 3）**：`users.role` 三档 —— `user`（默认）/ `authorized_user`（**被授权写公共库**）/
  `admin`（全部权限）。定义只留一份在 `utils/roles.py`：那边**不 import streamlit / MySQL**，
  所以 `api/deps.py`、`api/schemas.py` 可以直接复用（与 `utils/passwords.py` 同一套理由）；
  `migrations/003_user_role_authorized.sql` 的 ENUM 与之**逐字对齐**，有单测盯着两处不许漂移。
  能力由后端算好、随 `UserOut.can_write_public` 一起下发，前端只消费布尔值——
  授权规则抄第二份，就会出现"界面给了选项、点下去才 403"。

#### 权限矩阵（role × 操作）

| 能力 | `user` | `authorized_user` | `admin` |
| --- | --- | --- | --- |
| 读公共库（问答 / 列文件 / 读历史） | ✅ | ✅ | ✅ |
| **写**公共库（上传 / 删除公共文档） | ❌ 403 | ✅ | ✅ |
| 读写**自己的**私人库 | ✅ | ✅ | ✅ |
| 读写**他人的**私人库 | ❌ 403 | ❌ 403 | ❌ 403（隐私红线：管理员也不行） |
| **下载**他人的私人库原件（`GET /api/files/{uuid}/download`） | ❌ 403 | ❌ 403 | ❌ 403（同一条红线；公共库则登录即可） |
| 授权他人（`PUT /api/admin/users/{uuid}/role`） | ❌ 403 | ❌ 403 | ✅ |
| 提交反馈（`POST /api/feedback`，含「资料缺失」） | ✅ | ✅ | ✅ |
| 运营看板统计（`GET /api/admin/stats`） | ❌ 403 | ❌ 403 | ✅ |
| 管理端其它端点（`/api/admin/*`） | ❌ 403 | ❌ 403 | ✅ |

三条容易踩空的规则（每条都有单测）：

1. **授权 ≠ 提权**：`authorized_user` 只多拿到"写公共库"，拿不到"授权他人"——
   否则被授权者一步就能把自己升成管理员（`tests/unit/test_upload_scope.py`）；
2. **改角色必须吊销对方会话**：会话里存的是**登录那一刻**的角色，不吊销的话"降级"
   要等对方下次登录才生效——中间那段时间他手上仍是旧权限，最容易被误认为已经修好的漏洞；
3. **改不了自己，也动不了最后一名在册管理员**：前者让"界面角色 / 后端能力"错位，
   后者会让系统进入"再也没有人能授权"的死锁（两道闸门都在 `auth.set_user_role`）。
* `rerank_model` 用 `Literal` 白名单：否则客户端能指定任意 HF 仓库名让我们去下载。
* 报告下载做了路径穿越防护（只允许 `evaluation_output/` 下的文件）。
* 注册接口**忽略** body 里的 `role`：`create_user` 本身支持 role（脚本建管理员要用），
  所以"注册只能是普通用户"必须在路由层显式约束，否则谁都能把自己注册成管理员。

### SSE 事件契约（`POST /api/chat/stream`）

| event | data | 时机 |
| --- | --- | --- |
| `meta` | `{scope, scopes, skipped_scopes, question, rewritten_question, rewritten, rewrite_skipped, cached?, refused?}` | 开始 / 命中缓存 / 判定拒答 |
| `token` | `{text}` | 答案增量（每个字/词一帧） |
| `citations` | `{citations: [{source, page, scope, rerank_score, file_uuid, excerpt}]}` | 生成结束且非拒答 |
| `debug` | `{debug_info: [{length, is_parent, preview, rerank_score, scope}]}` | 生成结束且非拒答 |
| `done` | `{question, rewritten_question, answer, citations, scope, scopes, cached, max_score, refused, q_hash}` | 结束 |
| `error` | `{message}` | 流中途出错（状态码此时已发出，只能靠事件报错） |

模块 4 补充的四个字段（都是**旁观字段**，老前端忽略即可，不会因为不认识而报错）：

* `scope`：**库 key**，单库时就是 `public` / `private:<uuid>`（与缓存、历史同一个 key），
  多库时是 `public+private:<uuid>`；
* `scopes`：本次**真正检索过**的库列表（空库已被剔除，顺序固定 `[public, private:<uuid>]`），
  前端据此渲染「检索范围：公共库 + 我的文档」；
* `skipped_scopes`：因为**还是空库**被跳过的库（新用户第一次用 `both` 时必然出现），
  排障时一眼能看出"不是没搜到，是那个库还没有文档"；
* 引用 / 调试块上的 `scope`：这条证据来自哪个库。**同一个文件同时存在于两个库时会出现两条**，
  这是刻意的——合并掉就等于把"证据来自哪个库"丢了。

模块 6 补充的 `done.q_hash`（同样只增不改，老前端忽略即可）：

* 问题归一化（折叠空白、英文小写）后的 SHA-1（40 位十六进制）——👍/👎 的**幂等键**；
* 由**后端**算（与 `POST /api/feedback` 判重用的是同一个 `feedback_service.question_hash`），
  前端只负责拿它当 key。让前端自己算就得在浏览器里再实现一套归一化 + SHA-1，
  两份实现漂移的表现是"反馈过了按钮还能点，点了又插一行"。

多行答案会被 JSON 转义进**同一帧**——SSE 靠换行分帧，直接拼正文会把一帧拆散，
前端拼接就错乱。nginx 反代需保留 `X-Accel-Buffering: no`（代码里已带），
否则"流式"会退化成"转圈很久然后一次吐完"。

### 文件管理与下载（模块 5）

「文件管理」面板（左侧栏「📁 文件管理」）+ 引用溯源弹窗（点引用标签 → 文件名 / 页码 / 置信度 / 命中片段 / **查看原文** / 下载原文），
四块能力对应四个新东西：**留存原件**、**面板列表接口**、**下载接口**、**在线预览接口**（2026-10-13 验收新增）。

#### 1) 原件留存：`services/file_store.py`

改造前**没有任何地方保存上传的原件**：`services/pdf_service` 把字节写进 `tempfile`，
解析完在 `finally` 里 `os.unlink`，`file_dedup` 只剩元数据 ——
所以"下载原文"不是"少写了个端点"，而是没有东西可下。现在：

* 上传时先把原件落到 `<file_store_dir>/<file_uuid><ext>`（默认 `<base_dir>/file_store`，
  配置项 `FILE_STORE_DIR`；目录已 gitignore）；
* 用 `file_uuid` 命名而不是原名：同名文件（不同库/不同版本）不会互相覆盖，
  文件名里的 `../` 之类也进不了路径（原名只留在 DB 里做展示与 `Content-Disposition`）；
* 顺序是**留存 → 入库**：留存失败就直接 400，不会留下"入库了却永远下不到原件"的记录；
  入库失败/删除文件时，留存件会被一起清掉（`remove_original`）；
* **历史原件兜底**：改造前入库的 3 个公共库文档，原件其实一直在项目根目录
  （`FILE_LEGACY_SEARCH_DIRS`，默认就是 `base_dir`）。找回策略是"留存件优先，
  再按**文件名精确匹配**去兜底目录找"：同名多份时**返回 None**（宁可 404，
  也不能让用户下到别人的那份），只有精确命中才认。

#### 2) 面板列表：`GET /api/files/library`

为什么单独开一个端点而不是给 `GET /files` 加 `scope=both`：那个端点的语义是
"**某一个具体库**"（上传页的文件级过滤下拉逐个库去拉，`both` → 400 这条约定写在
`deps.resolve_scope` 与单测里），而面板问的是"我现在能看到哪些原件"。
查哪些库由**后端**按会话决定（`[public, private:<会话里的 uuid>]`），
客户端传任何 scope 参数都改不了 —— 别人的私人库不是"前端不显示"，是**根本没查**。

* `downloadable` / `size_bytes` 如实反映"原件在不在磁盘上"（历史文件为 `false`，
  面板把下载按钮置灰并用 hover 说明原因，而不是让用户点了才吃 404）；
  `size_bytes` 取**真实可读字节数**（`file_store.readable_size`，**不**用 `os.stat` ——
  本机 `.xlsx` 的 stat 会多报，下载长度也会因此跑偏，见 §3 那条）；
* 上传人用户名**单独查一次再合并**：`file_dedup`（运行期建，MySQL 8 默认
  `utf8mb4_0900_ai_ci`）与 `users`（migration 001 的 `utf8mb4_unicode_ci`）
  **排序规则不同**，`LEFT JOIN` 会报
  `1267 Illegal mix of collations ... for operation '='` —— 实测整个面板 500、
  页面上只显示"文件列表加载失败"。这条有单测盯着（SQL 里不许出现 JOIN）。

#### 2.1) 版本管理：`version` / `is_archived`（模块 5b）

`migrations/004_file_versioning.sql` 给 `file_dedup` 补了两列（`updated_at` 在 002 里本来就有）：

| 列 | 作用 |
| --- | --- |
| `version INT DEFAULT 1` | 同名文件在同库中是第几个版本（1 基） |
| `is_archived TINYINT(1) DEFAULT 0` | 1 = 已被同名新版取代（历史版本） |
| `idx_scope_name_version` | 服务"面板默认只列最新版本"（`WHERE scope IN (..) AND is_archived = 0`） |

* **同库同 MD5 = 去重**（2026-10-13 验收修正）：上传时先 `check_duplicate(md5, scope)`，
  已有相同内容的成功记录 → 回 200 + `duplicate=true`（「已入库，无需重复上传」），
  **不新建版本、也不归档旧版**。判据必须落在**内容**上：同名 + 同内容只是"又传了一遍"，
  记成 v2 会让面板并排出现两条一模一样的记录（v1 还会被打上「历史版本」）；
* **同名 + 内容不同 = 新版本**：`api/routers/files.py` 再算 `version = 同名最新 + 1`，
  入库**成功之后**才把同名旧记录标成 `is_archived = 1`。
  顺序不能颠倒：先归档再入库，一旦入库失败就会留下"旧版已归档、新版不存在"的空档
  （用户看到自己刚传的东西消失，而那份旧的其实还在库里）；
* **改造前、以及"改造后第一版"都不对的那一条**：最早 `check_duplicate` 按 **MD5** 一视同仁地拒收，
  于是"更新同名文档"（手册改一版、简历换一版）会被回一句"文件已入库，无需重复上传"；
  后来放宽成"同名就算新版本"，又把**同一份文件传两遍**也记成了 v2。
  现在的判据（**先内容、后文件名**）是：
  * 同库已有**相同 MD5** → 去重（不新建版本、不归档旧版）：不管文件名是否相同，
    同一份文件传两遍只是用户没看到上一次的提示，不是"换了一版"；
  * 同库**同名 + 内容不同** → 新版本 `v(N+1)`，旧版转历史版本；
  * **不同名 + 内容不同** → 就是两个文件，各记一条（文件名不同本来就不该互相顶掉）；
* **归档不删数据**：旧版本的 ES 块、向量、留存原件都还在——打开「显示历史版本」就能看、能下载，
  也能删（删除路径特意带 `include_archived=True`，否则归档版本会变成"看得见却删不掉"的幽灵数据）；
* **默认只列最新版本**：`GET /api/files?include_archived=` 与
  `GET /api/files/library?include_archived=` 默认 `false`。侧栏「文件级过滤」下拉依赖前者，
  把历史版本也放进去就会出现两个同名选项（uuid 不同），用户选中旧版后检索到的还是旧内容，
  看起来像"新上传的没生效"；
* **版本号来自库里那一列**，不再由列表层按顺序累加：累加只在"列表恰好按 `created_at` 排序"时才对，
  而面板现在要的是"同名版本聚在一起、新版在前"
  （`ORDER BY scope, file_name, version DESC, id`）；
* 老入口（Streamlit 版 `app.py` 不传版本号）由 `file_state.create_pending(version=None)` 兜底算
  `max + 1`，保证"同一个 (库, 文件名) 下版本号不重复"这条不变量与入口无关。
  ⚠️ 但 Streamlit 版**不做归档**：它的上传路径仍是老语义（同名文件各占一行、都算最新版），
  模块 5 的版本管理只落在 REST API 上；
* 面板显示 `v2`；历史版本再加一个灰色「历史版本」标签 + 整行变浅（`onRow` 给 style），
  一眼能看出"这一行不是当前版本"，不用逐行读标签。

#### 3) 下载：`GET /api/files/{file_uuid}/download`

* 顺序是**先取记录、再判权限**：权限判断不在 scope 字符串上，而在"记录里的 scope + 会话用户"
  之间。`public` → 登录即可；`private:<owner>` → 只有 owner 本人
  （**管理员也不行**：管理端看不到别人的私人库，放行只会造出一条"知道 uuid 就能读别人文档"的通道）；
* **403 与 404 分开**：无权 → 403 `无下载权限，请联系管理员`；记录不存在 → 404；
  记录在但原件丢了（改造前上传）→ 404 且文案说清是"未保留原件，请联系管理员重新上传"。
  把"无权"报成"不存在"会让用户拿着刚看到过的引用反复排查；
* 中文文件名交给 Starlette 走 RFC 5987（`filename*=utf-8''…`），不会乱码也不会因非法 header 崩掉；
  响应带 `Cache-Control: private, no-store`（私人库也用这个端点，不许中间层缓存）；
* **`Content-Length` 取"真实可读字节数"，不取 `os.stat`**（2026-10-13 修「Excel 下载失败」）：
  本机对 `.xlsx` 的 stat 会**多报**（实测同一份 12898 字节的文件 `stat = 20480`，
  `%TEMP%` 里也一样 —— 与目录无关、与扩展名有关），而 `FileResponse` 默认正是拿 stat 当
  `Content-Length` → 响应声明 20480、实际只发得出 12898 字节，浏览器收尾时连接断掉、
  下载栏里弹「失败 - 无法下载」。PDF 的 stat 与真实长度恰好一致，所以现象是"只有 Excel 下不了"，
  与权限 / MIME / 二进制读取都无关（存留件本身逐字节完好，MD5 与源文件一致）。
  现在下载与预览**共用** `_original_response`，长度一律来自 `file_store.readable_size`
  （`seek(0, SEEK_END) + tell()`）；面板的 `size_bytes` 走同一个口径。
  有单测盯着这条不变量（`tests/unit/test_file_download.py` 的 `TestOfficeOriginalIsServedWhole`，
  用 `tests/conftest.py:lying_stat` 确定性地复现"stat 多报"）；
* 前端不用 `<a href>` 直接下载：401/403/404 时后端回 JSON，浏览器会**导航到那串 JSON**
  （页面回不去）。改成 `fetch` → 失败弹中文提示，成功再用 `Content-Disposition` 里的名字
  生成 blob 下载（`client.ts:downloadFile` / `filenameFromContentDisposition`）。

#### 3.1) 在线预览：`GET /api/files/{file_uuid}/preview`（2026-10-13 验收新增）

* **权限与下载是同一份代码**（`_row_and_original`）：能预览一份文件就等于能读到它的内容，
  两个入口的规则必须一致，否则会出现"预览放行了、下载没放行"这种极难被人工发现的错位。
  只有 403 文案按动作区分（「无下载权限…」/「无预览权限…」）；
* 返回 `FileResponse(..., content_disposition_type=...)`：**白名单内**的类型（PDF / 文本 / 常见图片）
  给 `inline`，浏览器用自己的查看器就地打开——引用弹窗还会在 URL 上带 `#page=N`，
  直接翻到引用所在的那一页（页码是查看器的能力，接口**不**多收参数）；
* **白名单以外一律 `attachment`**：HTML / SVG / XML 能被浏览器当页面渲染，而 blob URL 与直接导航
  都**继承本站 origin**——内嵌它们等于让上传者在本站执行脚本。前端只看响应的
  `Content-Disposition` 决定"开标签页"还是"落盘下载"（**不**维护第二份类型名单，一份规则只留一处）；
* 前端 `client.ts:previewFile` 的降级路径：拿到 `attachment` → 直接下载；`window.open` 被拦 → 下载；
  两种情况都提示「无法在浏览器里直接预览，已改为下载」。浏览器查看器打不开某个 PDF（损坏 / 加密）
  时前端探测不到——所以弹窗里「下载原文」始终保留作兜底。

#### 3.2) 删除：`DELETE /api/files/{file_uuid}?scope=`（2026-10-13 验收新增）

* **五件事一起清**，顺序是 **语义缓存 → ES 父子块 → Chroma 向量 → MySQL 记录 → 留存原件**：
  记录放最后删，前序步骤失败时文件仍在列表里、用户可重试；反过来先删记录，一旦 ES 报错
  就会留下"搜得到、却怎么都删不掉"的孤儿块。ES 用 `delete_by_query` 按
  `metadata.file_uuid + metadata.scope` 一次删掉父块与子块（`refresh=True`，删完立刻搜不到）；
  Chroma 按 `source` 删向量（`owner__file_uuid__文件名`，天然按版本唯一，不会误伤同名的另一版）；
* **语义缓存排在第一个清**（2026-10-14 用户报的事故）：缓存 key 只由 scope + 版本号 +
  问题哈希决定，看不见"库里的文件被删了"，于是删除后用户再问同样的问题会命中缓存、
  回答继续引用已删除文件 —— 点引用下载 **404**。`redis_service.invalidate_cache_by_file_uuid`
  扫一轮 `cache:*`，按 payload 里的 `source_file_uuids`（`set_cached_answer` 从 citations
  抽出的 `file_uuid`，去重去空）逐条删除，返回条数并打 `cache_invalidated` 日志；
  **读不出来的脏 payload 一律保留**（少删只是慢一次，错删是丢缓存）。它排在 ES 之前还有一个
  理由：Redis 不可用时这里就抛异常 → 整次删除失败、文件仍在（用户可重试），
  而不是留下"文件已删、缓存还在"这种最难收场的状态。
  边界：本次改造前的缓存、以及 Streamlit 入口写的引用（只有 `source`/`page`）没有 uuid，
  精确失效覆盖不到 —— 靠 `KB_VERSION` 抬到 `v6` 把老缓存一次性作废（`services/redis_service.py`）；
* **权限与上传同一套**（`deps.resolve_scope(write=True)`）：`public` 需 admin / authorized_user，
  `private:<自己>` 仅本人，`both` → 400；普通用户删公共库文件 → 403，记录不在该库里 → 404。
  **历史版本也能删**（查询带 `include_archived=True`），否则打开「显示历史版本」后会看到
  "能下载却删不掉"的幽灵行；
* **界面上按钮的可见性由后端算**：`GET /api/files/library` 每行带 `can_delete`
  （`routers/files._can_delete` 把 `resolve_scope(write=True)` 跑一遍）——前端不按
  `role` / `owner` 自己判，权限规则只留一处，不会出现"给了按钮、点下去才 403"
  （也不会反过来：有权限却没入口）；
* **二次确认**：面板「删除」走 `AntdApp.useApp()` 的 `modal.confirm`，弹窗里写明文件名与
  "不可撤销"；确认成功后弹「已删除」并**重新拉一次列表**（以后端为准，不在前端本地 filter）；
* 失败文案按状态码给：403「无权限删除该文件」/ 404「文件不存在，可能已被删除」/
  500「删除失败，请稍后重试」（`client.ts:deleteFileErrorMessage`）——后端 403 的 `detail`
  是 `resolve_scope` 共用的那句（上传口径："…可以写入公共库…"），直接用会让人看不懂；
* 删除**不碰检索日志**：`retrieval_log` 里那个 `file_uuid` 仍然保留（它确实被检索过），
  运营看板按空文件名显示（`tests/unit/test_admin_stats.py` 有对应断言）。

#### 4) 引用溯源弹窗

引用要带 `file_uuid`（定位原件）与 `excerpt`（命中片段，压空白后截 240 字）——
没有它们，弹窗只能重复"文件名 + 第 N 页"，用户还得自己翻。
同一 `(文件, 页, 库)` 被多个子块命中时，分数与摘录一起换成**最高分那次**的（不许"高分 + 差摘录"自相矛盾）。
**老缓存里的引用没有这两个字段**：弹窗如实说明"来自旧缓存，未保存摘录 / 无法定位原件"
并禁用下载 —— 不拿文件名去猜 uuid（猜错就是把别人的文档下给用户）。



### 反馈飞轮 + 运营看板（模块 6）

#### 1) 反馈：`POST /api/feedback` + `q_hash` 防重

* 前端在**每条 AI 回答**下方给 👍/👎；提交后那一条就地变成「已反馈」；
* 幂等键是 `q_hash`（问题归一化后的 SHA-1，40 位十六进制）：由**后端**
  （`feedback_service.question_hash`）算，随 `done` 事件给到前端，前端只拿它当 key；
* 服务端在**10 分钟窗口内**对同一个 `(user, q_hash)` 走 UPDATE 而不是 INSERT
  （`DEDUPE_WINDOW_MINUTES`）：连点两次 👎 只算一条（否则看板上的差评数直接翻倍），
  而"同一个问题两周后又被差评"是**真信号**（同一处资料一直没补），不该被合并掉。
  UPDATE **不刷新 `created_at`**——那是这条记录第一次出现的时间，被冲掉会让"最近差评"的排序乱跳；
* `reason` 是差评原因；`GET /api/feedback/bad-cases` 按时间倒序给管理员看。

#### 2) 检索流水：`retrieval_log`（看板的数据源）

改造前 `api/routers/chat.py` 只在 SSE 里发了一个 `no_result` 就随连接消失了，
所以看板要的四个数字里**有三个根本没有落盘**（高频提问 / 被检索最多的文档 / 检索无结果）。
`migrations/005_feedback_retrieval_log.sql` 因此新增一张表，每次问答记一行：
`question` / `rewritten_question` / `hit_count` / `max_score` / `no_result` / `file_uuids`。
三条硬约束（每条都有单测）：

1. **绝不抛异常**：写流水是旁路（`services/retrieval_log_service.record` 内部全包），
   MySQL 抖一下不能毁掉一次问答——那对用户就是"提问失败"；
2. **命中语义缓存的那次不记**：那次压根没检索。记的话"被检索最多的文档"会变成
   "缓存命中最多的文档"（数字看着正常、含义已经错了，肉眼发现不了）；
3. **不 JOIN**：`file_dedup` 在交付机上是运行期建的（`utf8mb4_0900_ai_ci`），
   与 migration 建的表（`utf8mb4_unicode_ci`）一比就报 1267 —— 统计一律"分开查 + Python 合并"
   （`file_state.names_for_uuids`）。流水存的是**原始分**（不是发给前端的"哨兵值一律 None"那一版）：
   0.42 与"没做分数门控"在排障时必须分得开。

#### 3) 看板：`GET /api/admin/stats`（admin）+ `/admin` 页面

| 块 | 数据来源 | 说明 |
| --- | --- | --- |
| `top_questions` | `retrieval_log` | 高频提问（按归一化后的问题聚合；次数降序、并列按问题升序——排序必须确定，否则每次刷新都在跳） |
| `top_documents` | `retrieval_log.file_uuids` | 被检索最多的文档（**一次检索里同一个文件只算一次**；文件被删了名字回空串，但**不把行删掉**——它确实被检索过） |
| `no_result_queries` | `retrieval_log.no_result` | 检索无结果的问题——最该补资料的信号 |
| `feedback_summary` | `feedback` | 好评/差评、按库分布、差评原因 Top 5（`up_rate` 为 `null` = "还没有反馈"，**不是** 0%） |

* 统计的是**最近 `window` 次检索**（默认 5000，接口下限 50）：流水会一直涨，看板要的是最近的运营状况，
  也让聚合成本可控；页头写着「数据截止 …（统计最近 N 次检索）」，避免把快照当成实时值；
* 聚合在 Python 里做（窗口内有界）：`JSON_TABLE` 换个 MySQL 小版本就可能报错，
  而它挂掉的表现是"看板整页 500"；
* 接口失败回 **500** 而不是空数据：四个块全 0 与"统计读不出来"在界面上必须分得开；
* 前端 `/admin`（`<RequireAuth adminOnly>`）用 Ant Design `Card` + `Table` 铺开上面四块，
  另加「按库分布 / 差评原因 Top 5 / 最近差评样本」三张表；非管理员看到的是
  「需要管理员权限」+ 「返回对话」，而不是一个加载失败的红条（后端才是权限的权威）。

#### 4) 前端入口

| 位置 | 行为 |
| --- | --- |
| AI 回答下方 | 👍 有帮助 / 👎 没帮助。**回答定稿后**才出现（打字中、或这次出错时不评：给半截答案打分、把服务故障记成"答得不好"都会污染运营判断）；提交后就地变「已反馈」 |
| 输入框右侧 | 「资料缺失/反馈」→ 弹窗（问题默认填**最近一次提问**、可改；另有补充说明），提交成 `rating=down` + `reason=资料缺失…`。没有对话时按钮禁用 |
| 侧栏 | 「📊 运营看板」（仅管理员）→ `/admin` |

> 弹窗提交与回答下方的 👎 是**同一条记录**（后端按 `q_hash` 判重）：问题默认值不改成别的问题，
> 两处入口的状态就永远一致（这也是 E2E 里那条断言的前提）。

### 前端要点（`rag-frontend/`）

| 位置 | 行为 |
| --- | --- |
| 顶栏 | **修改密码**（模块 2.1）：弹窗三字段（当前 / 新 / 确认），前端校验 ≥8 位、两次一致、≠当前密码；成功后后端吊销全部会话 → 提示"密码已修改，请重新登录"并跳 `/login` |
| 登录页 | **注册入口**（模块 2.2）：挂载时调 `GET /auth/config`，`allow_register=true` 才显示"没有账号？立即注册"；拿不到配置按**关闭**处理（安全默认）；注册成功提示"注册成功，请登录"并把用户名回填 |
| 侧栏「检索范围」 | 三选项（公共库 / 我的私人库 / **两者一起，默认**）；`both` 只在问答/历史成立，列文件时前端把它展开成两个库各查一次并给文件名加库标 |
| 侧栏「检索设置」 | 重排模型默认 **`bge-reranker-base`（中文推荐 · 默认）**，MiniLM 保留为备选（`DEFAULT_RERANK_MODEL` 常量，与后端 `utils.rerank.DEFAULT_RERANK_MODEL` 同源；**不**用 `RERANK_MODELS[0]` 表达默认值） |
| 回答气泡 | 上方一行「检索范围：公共库 + 我的文档」（取自 `meta/done` 的 `scopes`，空库已被后端剔除）；**`skipped_scopes` 非空时追加「（我的私人库为空，已跳过）」**——否则用户会以为自己的文档根本没被检索；`both` 模式下引用标签追加「（公共库）/（我的文档）」 |
| 侧栏「📁 文件管理」 | 模块 5：可折叠面板，列出**公共库 + 我的私人库**的原件（文件名 / 库 / 版本 / 更新时间 / 上传人 / **预览 / 下载**；后端 `can_delete` 为真时再多一个**「删除」**（2026-10-13），点它先弹二次确认、确认后自动刷新列表），上传成功后自动刷新；没留存原件的行按钮置灰并在 hover 说明原因；**「显示历史版本」开关**（模块 5b）默认关闭，打开后历史版本带「历史版本」标签并整行变浅；**「预览」**（2026-10-13）在新标签页里打开原件，自己**不**判类型（只看后端 `Content-Disposition`：`attachment` 时降级成下载并提示） |
| 引用标签 | 模块 5：可点击（`cursor: pointer` + hover 提示）→ 「引用出处」弹窗（文件名 / 页码 / 置信度 / 命中片段 / **查看原文** / 下载原文）；**「查看原文」**（2026-10-13）在预览 URL 上带 `#page=N`，直接翻到引用那一页；老缓存引用没有文件标识 → 如实提示并禁用两个按钮 |
| AI 回答下方 | 模块 6：👍 有帮助 / 👎 没帮助（回答定稿后才出现）→ 提交后就地变「已反馈」；输入框右侧另有「资料缺失/反馈」弹窗（默认带上最近一次提问） |
| 侧栏「📊 运营看板」 | 模块 6：**仅管理员可见** → `/admin`（`RequireAuth adminOnly`）：概览 4 个数字 + 高频提问 / 无结果问题 / 高频文档 / 反馈汇总 / 按库分布 / 差评原因 / 最近差评样本 |
| 前端类型 | `ScopeMode` / `SCOPE_MODE_LABELS` / `scopeLabel()` / `describeScopes()` / `describeSkippedScopes()` 都在 `src/api/client.ts`，与后端 `deps.describe_scope` 同一份口径（改文案时两边一起改）；模块 5 的 `ApiLibraryFile` / `listLibraryFiles()` / `downloadFile()` / `previewFile()` / `deleteFile()` / `deleteFileErrorMessage()` / `formatFileSize()`、模块 6 的 `submitFeedback()` / `getAdminStats()` / `listBadCases()` 也在同一文件 |

前端 E2E 见上文「测试」一节（Playwright，`rag-frontend/e2e/*.spec.ts`）。

#### 升级已有部署（模块 5 / 6 的表结构）

老库里没有 `version` / `is_archived` / `feedback.q_hash`，也没有 `retrieval_log`。
用这一条补齐（**可反复执行**，只补缺的部分）：

```bash
# 先停后端（在线写入与 DDL 撞在一起时失败信息很难看），然后：
.venv\Scripts\python.exe scripts\apply_schema_updates.py --dry-run   # 先看要改什么
.venv\Scripts\python.exe scripts\apply_schema_updates.py
```

* 存量数据不受影响：改造前上传的文件全部拿到默认值（`version=1`、`is_archived=0`），
  也就是"每个文件各自 v1、都是最新版"，与改造前的界面表现一致；
* **不补也能跑**（只是没有新功能）：`_library_rows` 对缺失的列按 `v1` / 非归档处理，
  面板不会因为"列还没加上"而整页报错——部署顺序上先起服务再补列也不会白屏；
* 反过来，`retrieval_log` 不存在时看板会回 500（而不是显示一片 0），
  所以**要用运营看板就必须执行这一步**。

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

## ⚠️ 已知限制（Known Limitations）

### 1. Chroma 只支持**单进程**访问一个持久化目录

**现象**：`chromadb` 抛 `hnsw segment reader: Nothing found on disk`
（日志里表现为上传报错、或"文件删不掉 / 删了还能搜到"）。

**根因（2026-10-12 本机实测，chromadb 1.5.9 + Windows）**：

* 这句话**不在 chromadb 的 Python 代码里**（在装好的 `chromadb` 包里全文搜索零命中），
  它来自 **Rust 原生索引实现**，对应"HNSW 段落文件（`<chroma_*>/<uuid>/*.bin`）缺失或只写了一半"这一状态；
* 该状态的两个现实来源：① **多个进程同时写同一个目录**（uvicorn `--workers N`、
  服务与 `scripts/*` 或 Streamlit 版并行、Docker 多副本）；② 写入过程中进程被**强杀**；
* 对照实验（`chroma_probe.py` / `chroma_probe2.py` / `chroma_probe3.py`，全部在临时目录里做）：

  | 场景 | 结果 |
  | --- | --- |
  | 父进程持锁时子进程读 | ✅ 正常 |
  | 父进程 close 后子进程读 | ✅ 正常 |
  | 两个进程**交错写**同一目录后再读 | ✅ 正常（1.5.9 能合并） |
  | 写入中途**强杀** → 另一个进程打开 | ⚠️ **不报错，库却变成空的**（`chroma.sqlite3` 都没落盘） |
  | 同进程两个客户端指向同一目录 | ✅ 正常 |
  | 只有 `chroma.sqlite3`、没有段落目录（本仓库里真实存在这种库） | ✅ 正常（count / get / query 都能跑） |

  结论：**在 1.5.9 上，这条报错已经不是"跨进程读"的必然结果**（chromadb < 1.x 时代它是常见故障）；
  真正还在的风险是**"并发写 + 强杀"导致的静默损坏**——比报错更难查，因为日志里什么都没有。
  本项目 `services/vector_service.py` 每次调用都会 `new` 一个 `Chroma(persist_directory=...)`，
  所以"同一进程多客户端"是常态（实测安全），但**跨进程并发写依然必须避免**。

**处置（按优先级）**：

1. **单进程**：`uvicorn --workers 1`（上文已经是硬要求）；
2. **不要并发写**：跑 `scripts/reset_knowledge_base.py`、评测脚本或 Streamlit 版之前先停 API 服务；
   容器化时给脚本单独的 `CHROMA_BASE_DIR`，别和服务共享同一个卷；
3. **优雅停止**（Ctrl+C / `docker stop`，不要 `kill -9`、不要直接关窗口），
   否则最后一次写入可能整体丢失（实测目录会变成空的）；
4. 怀疑已损坏时：先停服务，逐个检查 `chroma_*/` 目录 ——
   ① 有没有 `chroma.sqlite3`；② 有没有段落子目录（`<uuid>/` 里应有
   `data_level0.bin` / `header.bin` / `length.bin` / `link_lists.bin`）。
   段落目录缺失在 1.5.9 上仍可读（那多半只是个还没写入过的空库）；
   **恢复手段是"重灌"**：`file_dedup` 里 `status=success` 的文件重新上传即可
   （ES 父块 + Chroma 向量一起重建），比手改二进制索引可靠得多。

### 2. 其它

* **上传是同步的**（Docling 解析 10~60s，见"迁移进度"）：大文件上传时前端要等，属已知取舍。
* **改造前上传的文件没有留存原件**：那时解析完就把临时文件删了（见"文件管理与下载"一节），
  现在只能按文件名去项目根目录兜底找。找得到就能下（本机交付环境那 3 个公共库文档属于这种），
  找不到就如实回 404「未保留原件，请联系管理员重新上传」——**不会**假装能下再报错。
* **留存原件会占磁盘**：目录是 `file_store/`（`FILE_STORE_DIR`），删除文件时会一起清掉；
  但"上传后从未删除"的文件会一直留着 —— 与向量库/ES 里的块同生命周期，不做单独的回收策略。
* **归档的历史版本仍占着磁盘与索引**（模块 5）：归档只改标记，ES 块 / Chroma 向量 / 留存原件都还在——
  这正是"历史版本能看、能下载"的前提，代价是"传了 10 个版本就是 10 份存储"。
  不再需要的版本请显式删除（面板打开「显示历史版本」→ 删除），删除会把它们一起清掉。
* **运营看板只统计最近 N 次检索**（模块 6，默认 5000，可用 `?window=` 调整）：这是刻意的
  （流水会一直涨，看板要的是最近的运营状况，聚合成本也才可控），页头会写出实际窗口。
  `retrieval_log` **不存答案正文**——要看答案请走 `feedback` 表（那是用户明确提交的内容）。
* **Streamlit 版的上传仍是老语义**（模块 5）：它不传版本号（服务层兜底编号）也不做归档，
  版本管理只在 REST API 上成立——迁移期两边并存时，别拿 Streamlit 上传验"同名 = 新版本"。
* 生成侧的两个已知残留问题（编造公司名 / 多写"理由"句）见下文第 21 条。
* **本机（公司加密机）上 `vite build` 会失败，`npm run dev` 正常**：
  `src/index.css` 这类前端源码对 `node.exe` 是**透明加密态**（文件头 `%TS`），
  vite 报 `UNLOADABLE_DEPENDENCY … stream did not contain valid UTF-8`；
  `tsc -b`（类型检查）不受影响，dev server 因为启动时已把文件读进内存也照常工作。
  需要真正构建时，照 `scripts/unlock_files.cmd` 的思路（用 git 读明文、用 cmd 重定向写盘）
  先把前端源码解成明文。另外 Playwright **自带的 chromium 被本机 Device Guard 策略禁止执行**
  （`spawn UNKNOWN` / "已被组织用 Device Guard 策略阻止"），
  所以 `playwright.config.ts` 默认用系统 Edge（`channel: 'msedge'`）。

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

    行为说明：**默认模型现在是 BGE（见文末 2026-10-13 那条：默认值与归一化一起改了）**；
    继续用轻量级 MiniLM 时会自动退化为"不精排"（保留 RRF 顺序），不会再劣化检索结果。

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
* ✅ **P0 三件事（2026-10-12，均已复现/实测后修完）**：
  1. **`print(emoji)` → logger**：`services/pdf_service.py`（3 处）、
     `services/retrieve_service.py`（1 处）、`services/es_service.py`（1 处重复日志）、
     `app.py`（3 处遗留）全部改走 `utils.logger`。原因不是"代码整洁"——GBK 控制台（cp936）
     下 `print("⚠️…")` 会抛 `UnicodeEncodeError`，而这些点正好在**首次上传 / 一次问答 /
     删除文件**的路径上，异常会把整次请求毁在中间（本次实测：任何带 emoji 的脚本在
     当前终端直接崩，报的就是 `'gbk' codec can't encode character '\u26a0'`）。
  2. **ES 删除计数**：真实 ES 8.11 对照实验（临时 file_uuid，造 3 条 → 删 → 数剩余）确认
     `delete_by_query` 的响应是 `ObjectApiResponse`，`deleted` = **真实删除数**、`total` = 命中数，
     另有 `version_conflicts` / `failures`。**老代码取值（`res.get("deleted")`）没写错**，
     真正的缺陷是语义不完整：版本冲突 / 分片失败时 `deleted < total`，却只记一条 info，
     等于把"删了一半"当成功上报。现在统一走 `_delete_by_query_stats`，
     `total != deleted` 或有 `failures` 时降级成 warning / error（附 7 条单测）。
  3. **Chroma 跨进程报错**：见上面「⚠️ 已知限制」第 1 条。
* **注册开关（模块 2.2）**：`config.py` 新增 `allow_register: bool = False`（默认关闭），
  登录页用 `GET /api/auth/config` 决定是否显示"没有账号？立即注册"；
  服务端 `POST /api/auth/register` 同样校验（关着 → 403），**只藏前端入口是藏不住 API 的**。
  演示时在 `rag_config.txt` 里加 `allow_register = true` 即可（`pytest` 用 monkeypatch 开关，
  不依赖本地配置）。
* **账户安全（2026-10-12 已补一半）**：自助「修改密码」已上线（侧边栏），但**还没有**
  「管理员重置他人密码」和「忘记密码」流程；注册 tab 仍是自助开放（`auth.require_login`），
  对外演示前建议加邀请码或关闭。**admin 的默认密码 `ChangeMe_123!` 建议第一次登录就改掉**
  ——现在有入口了，别再拖。
* **账户基线（实测）**：`users` 表只有 1 个账号（`admin` / role=admin / is_active=1）；
  MySQL root 是 `Lm060205.` 且应用也用它连库（生产建议单独建最小权限账号）；
  Redis 无密码、ES 未开认证，但都只绑 `127.0.0.1`。`.env` / `rag_config.txt` 均被 gitignore，
  且 `git log --all -S` 实测这两个真实密钥**从未进过任何提交**，所以无需因仓库泄露而轮换。
* ✅ **重排默认换成 BGE + 归一化按模型族（2026-10-13，实测后修完）**：
  * **默认值**：`utils.rerank.DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"` 是唯一真源，
    `api/schemas.py` 的 `ChatIn.rerank_model` 默认值、`RERANK_MODEL_CHOICES[0]`、以及前端
    `client.ts` 的 `DEFAULT_RERANK_MODEL` 三处都对齐它；MiniLM **保留为备选**（英文语料上它又快又准），
    只是不再作默认——`tests/unit/test_rerank.py::TestDefaultModelIsAligned` 与
    `e2e/rerank-default.spec.ts` 分别钉住后端与界面上那两份。
  * **归一化**（`utils/rerank.normalize_scores(raw, model_name)`）：两个模型实测返回的东西**根本不同**——
    MiniLM 返回**原始 logits**（中文实测 -2.0 ~ +7.2），BGE 返回的**已经是概率**（0.000037 ~ 0.9993）。
    旧代码一个 sigmoid 走天下，两个方向都坏：

    1. MiniLM：raw 3 → 0.953、raw 8 → **0.9997** —— 用户看到的"置信度全是 1.00"就是这里来的；
    2. BGE：概率再 sigmoid 是**二次压缩**，0.000037 → **0.500009**，刚好卡在
       `api/routers/chat.py` 的 `CONFIDENCE_THRESHOLD = 0.5` 之上，于是"明显无关的块"也能照常回答。

    现在：logits 模型 → `sigmoid(x / 5)`（3 → 0.646、8 → 0.832，差 0.19 且离 1.00 很远，
    raw 40 也才 0.9996，不饱和）；概率模型 → 裁剪到 [0,1]。两种映射在 raw=0 处都恰好是 0.5，
    所以拒答阈值语义不变。刻意**不用 min-max**：它每批必有一个满分，置信度就失去跨查询可比性。
  * **饱和闸门刻意没动**：`is_informative` 仍用 T=1 的快照（最灵敏的饱和探测器）。实测同一批真实召回
    在 T=1 / T=5 下判决一致（日志里的 spread 都是 `0.000781`），但极端 logits（11/10/9）在 T=5 下
    会被误判成"有区分度"→ 门控宁可误判为不可信；单测为此留了一条反证用例。
  * **同一道题的真实前后对比**（`scope=public`，提问「张嘉亮的手机号和邮箱分别是多少？」，
    都走真实 ES + Chroma + Redis + LLM）：

    | 指标 | MiniLM（改前） | MiniLM（修完归一化） | BGE（改前） | BGE（修完） |
    | --- | --- | --- | --- | --- |
    | top-5 的 rerank_score | 0.9929 / 0.9991 / 0.9995 / **0.9998** / 0.9987 | 0.7289 / 0.8042 / 0.8193 / **0.8492** / 0.7907 | 0.7263 / **0.5000** / 0.5000 / 0.5000 / 0.5000 | **0.9758** / 0.0000 ×4 |
    | 前端显示的置信度 | 0.9998（假满分） | 0.8492 | 0.7263 | **0.9758** |
    | 正确答案块在 top-3 | 是（靠闸门退回 RRF 顺序） | 是 | 是 | 是 |

    注意 MiniLM 那一列**排序仍然是噪声**（换映射只改刻度不改顺序），中文语料的正解是默认的 BGE；
    闸门只负责"分不清时别乱排"。
* ✅ **Query Rewriting 过度改写（2026-10-13 修）**：实测问「**张嘉亮主修课程**」（无代词、非省略句）
  被 glm-4-flash 改写成「张嘉亮主修了哪些课程」——词面一动，BM25 那条臂的关键词命中就跟着偏，
  界面上还会冒出"已重写为：…"。现在闸门分两段：`query_rewrite_service.is_self_contained()`
  先做**规则判定**（不含代词/指代标记、句尾不是"呢/吗/吧/么"、去掉标点后 ≥6 字 → 直接跳过，
  **零 LLM 调用**），剩下的（真代词 / 省略句）才交给 LLM，且 prompt 已加严（只允许"代词替换"
  和"省略补全"两种改写，并在 prompt 里点名反例）。跳过原因通过 `meta.rewrite_skipped` 如实上报
  （`no_history` / `self_contained`）。
  同一 prompt 的 LLM 层 A/B（模型与历史相同，旧 prompt 取自 git HEAD 那版）：

  | 问题 | 旧 prompt | 新 prompt |
  | --- | --- | --- |
  | 张嘉亮主修课程 | `张嘉亮主修了哪些课程` ✗ 过度改写 | `张嘉亮主修课程` ✓ 原样（且规则层直接跳过） |
  | 张嘉亮的手机号是多少？ | `张嘉亮的手机号` ✗ 把问句截断了 | `张嘉亮的手机号是多少？` ✓ 原样 |
  | 他主修哪些课程？ | `张嘉亮主修了哪些课程？` ✓ | `张嘉亮主修了哪些课程？` ✓（真追问照旧消解） |

* ✅ **「检索范围」标注补全（2026-10-13 修）**：左侧选「两者一起」而私人库还是空的时候，
  后端早就在 `meta.skipped_scopes` 里说清了"私人库为空、这次没查"，但前端只显示「检索范围：公共库」，
  看起来像自己的文档根本没被检索。现在会补一句 →「检索范围：公共库**（我的私人库为空，已跳过）**」
  （文案拼装在 `client.describeSkippedScopes()`；`e2e/scope-both.spec.ts` 按**整串**断言）。
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



