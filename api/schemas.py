"""API 的请求/响应模型（Pydantic v2）。

约定：
* 字段名与前端 JSON 一致（snake_case，避免前端再写一层转换）；
* **不信任客户端传来的任何身份信息**——``user_uuid`` / ``role`` 一律来自会话，
  不出现在请求体里（注册也不允许指定 role，见 ``routers/auth.py``）；
* ``scope`` 由 ``deps.resolve_scope`` 校验，这里只做类型约束。
"""
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, computed_field

from utils.rerank import DEFAULT_RERANK_MODEL
from utils.roles import can_write_public

#: 可选重排模型（**顺序 = 推荐顺序，第一个是默认值**，与前端 ``client.ts``、
#: ``utils.rerank`` 三方一致）。
#: 中文语料用 BGE（实测区分度 0.9993 vs 0.000037）；MiniLM 是英文 MS MARCO 模型，
#: 中文上实测分数饱和（不相关块反而更高），保留给英文语料，不再作默认。
RERANK_MODEL_CHOICES = (
    DEFAULT_RERANK_MODEL,
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
)

#: 用 Literal 而不是自由字符串：否则客户端能让我们去 HF 加载**任意**模型仓库，
#: 每个名字都对应一次真实下载 + 常驻内存，是典型的资源滥用入口。
RerankModel = Literal[
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "BAAI/bge-reranker-base",
]


# --------------------------- 认证 ---------------------------
#: 角色字面量：取值必须与 ``utils.roles`` 的角色枚举**逐字一致**
#: （``tests/unit/test_user_roles.py`` 有一条断言钉死，改名时不会静默漂移）。
#: 这里刻意写字面量而不是引用常量：``Literal`` 里放变量会让静态类型检查失效——
#: 那等于没有约束，而"角色拼错一个字母"在后端就是 422/500 的区别。
RoleName = Literal["user", "authorized_user", "admin"]


class RegisterIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, description="用户名（≤64 字符）")
    password: str = Field(..., description="密码（≥8 位、≤72 字节，规则见 utils.passwords）")


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordChangeIn(BaseModel):
    old_password: str = Field(..., description="当前密码（必填：否则会话被劫持即等于账号被夺走）")
    new_password: str
    confirm_password: str


class UserOut(BaseModel):
    user_uuid: str
    username: str
    role: RoleName

    @computed_field  # type: ignore[prop-decorator]
    @property
    def can_write_public(self) -> bool:
        """能否写入公共库（= 能否上传/删除公共库文档）。

        为什么由后端算好给前端：让前端自己按 ``role`` 判断，就是把授权规则抄了第二份；
        哪天后端增删一个"可写公共库"的角色，前端会安静地给出错误选项（点下去才 403）。
        这里复用 ``utils.roles.can_write_public``，与 ``deps.resolve_scope`` 是同一个判断。
        """
        return can_write_public(self.role)


class UserRoleIn(BaseModel):
    """管理员改角色请求体（模块 3）。

    只有一个 ``role``：``user_uuid`` 走路径参数、操作者一律来自会话——
    请求体里永远不出现"我是谁"，与 ``RegisterIn`` 忽略 role 是同一条原则。
    """

    role: RoleName


class MessageOut(BaseModel):
    ok: bool = True
    message: str


# --------------------------- 文件 ---------------------------
class FileOut(BaseModel):
    """侧栏「文件级过滤」下拉的一项（``GET /files?scope=``）。

    模块 5 起多带 ``version`` / ``is_archived``：下拉里只列**最新版本**
    （``is_archived=0``），但把字段如实给出去，前端要标注"v2"或做二次判断时不用再猜。
    """

    file_uuid: str
    file_name: str
    source: str
    #: 同名文件的第几个版本（1 基，``file_dedup.version``）
    version: int = 1
    #: 是否已被同名新版本取代（历史版本）。默认查询不会返回它们。
    is_archived: bool = False


class LibraryFileOut(BaseModel):
    """文件管理面板的一项（``GET /files/library``）。

    与 ``FileOut`` 的区别：这个是**给人看的列表**，所以带上了上传人、版本、时间、
    原件是否还在（``downloadable``）。刻意不返回 ``source``：它是内部的唯一键
    （``owner__uuid__文件名``），面板上没用，而跟着 JSON 出去只会让人以为它有意义。
    """

    file_uuid: str
    file_name: str
    #: 所在库（``public`` / ``private:<uuid>``）与它的中文名（公共库 / 我的文档）
    scope: str
    scope_label: str
    owner_uuid: str
    #: 上传人用户名；账号已被清理时是空串（面板显示"—"）
    owner_username: str = ""
    #: 同名文件的第几个版本（1 基）。模块 5 起是 ``file_dedup.version``（**真实列**），
    #: 不再由列表层"数出来"——那样一旦改排序方式，版本号就会跟着乱跳。
    version: int = 1
    #: 是否为历史版本（已被同名新版本取代）。默认列表不含它，打开「显示历史版本」才出现；
    #: 归档**不删数据**：历史版本的 ES 块 / 向量 / 留存原件都还在，仍可下载（用户要的
    #: 正是"上一版长什么样"）。
    archived: bool = False
    #: 记录更新时间（``file_dedup.updated_at``，入库完成时会被 MySQL 自动刷新）
    updated_at: Optional[datetime] = None
    #: 原件大小；找不到原件时 None
    size_bytes: Optional[int] = None
    #: 原件是否找得到。False → 面板把下载按钮置灰并说明原因，
    #: 而不是让用户点了才吃 404（"改造前上传的文件没有留存件"）
    downloadable: bool = False
    #: **能不能删这一行**（2026-10-13 验收新增）。由后端按会话算好（见
    #: ``routers/files._can_delete``：它就是 ``resolve_scope(write=True)`` 的预演），
    #: 面板据此决定给不给「删除」按钮。
    #: 为什么不让前端自己判：那等于把授权规则抄第二份，失效形态是"界面给了按钮、
    #: 点下去才 403"（或更糟：明明有权限却不给入口），与 ``UserOut.can_write_public``
    #: 是同一条原则——**判断只留一份**。
    #: 默认 ``False``：老前端 / 老库缺这个字段时当作"不能删"，fail-closed。
    can_delete: bool = False



class UploadOut(BaseModel):
    ok: bool = True
    message: str
    file_uuid: Optional[str] = None
    duplicate: bool = False
    #: 本次入库的版本号（模块 5）。同名文件重复上传时是 旧版本 + 1，前端据此
    #: 提示"已作为 v2 保存"；老调用方（Streamlit 版）不看这个字段，保持可空。
    version: Optional[int] = None


# --------------------------- 问答 ---------------------------
class ChatIn(BaseModel):
    question: str = Field(..., min_length=1, description="用户问题")
    #: 模块 4：``both`` = 公共库 + 自己的私人库一起检索（只读语义）。
    #: 仍是 str 而不是 Literal：这里只做类型约束，**合法性一律由 deps.resolve_scope_set
    #: 判定**（越权 scope 要回 403/400 并带中文原因，Literal 会让它变成没有文案的 422）。
    scope: str = Field(
        "public",
        description="public、private:<user_uuid> 或 both（both = 公共库 + 我的私人库）",
    )
    enable_rerank: bool = True
    #: 与 ``utils.rerank.DEFAULT_RERANK_MODEL`` / 前端 ``DEFAULT_RERANK_MODEL`` 同源：
    #: 不传这个字段的调用方（curl、评测脚本、老前端）也拿到中文可用的那个默认值。
    rerank_model: RerankModel = DEFAULT_RERANK_MODEL
    file_uuid: Optional[str] = Field(None, description="限定检索单个文件；None = 全部")


class Citation(BaseModel):
    source: str
    page: int
    #: 模块 4：这条引用来自哪个库（``public`` / ``private:<uuid>``）。
    #: 可空是为了兼容"老前端 + 老响应"以及历史缓存里存过的旧引用（没这个字段）。
    scope: Optional[str] = None
    #: 溯源弹窗要的两样（文件管理 / 下载）：
    #: ``file_uuid`` 用来定位原件（下载接口只认它），``excerpt`` 是这次命中的那段原文。
    #: 都可空——**老缓存里的引用没有它们**，那时弹窗如实说"未保存摘录 / 无法定位原件"，
    #: 不编内容、也不假装能下载。
    file_uuid: Optional[str] = None
    excerpt: Optional[str] = None
    #: 这条引用的精排分数（前端据此做引用级过滤与置信度展示）。
    #: 写进模型是为了**反馈回传时不再被静默丢掉**（此前它只是 SSE 里的一个裸字段，
    #: 走 ``FeedbackIn.citations`` 校验时会被 pydantic 丢掉，bad case 里就查不到分数了）。
    rerank_score: Optional[float] = None



class AuthConfigOut(BaseModel):
    """登录页需要的公开开关（不需要登录即可获取）。

    刻意只放"前端渲染要用、且不敏感"的开关：一旦这里开始返回内部配置，
    它就成了一个免登录的配置泄露入口。
    """

    allow_register: bool = False


class DebugChunk(BaseModel):
    length: int
    is_parent: bool
    preview: str
    rerank_score: float


class ChatResult(BaseModel):
    question: str
    rewritten_question: str
    answer: str
    citations: List[Citation] = []
    scope: str
    cached: bool = False
    max_score: float = 0.0
    refused: bool = False


# --------------------------- 历史 / 反馈 ---------------------------
class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class FeedbackIn(BaseModel):
    scope: str = "public"
    question: str
    rewritten_question: Optional[str] = None
    answer: Optional[str] = None
    citations: List[Citation] = []
    rating: Literal["up", "down"]
    reason: Optional[str] = None
    file_uuid_filter: Optional[str] = None


class BadCaseOut(BaseModel):
    id: int
    user_uuid: str
    scope: str
    question: str
    answer: Optional[str] = None
    rating: str
    reason: Optional[str] = None
    # MySQL DATETIME → pydantic 直接吃 datetime，序列化成 ISO 字符串给前端
    created_at: Optional[datetime] = None


class FeedbackOut(BaseModel):
    """``POST /feedback`` 的返回：把服务端算出的 **q_hash** 回给前端。

    为什么必须回它：前端用它做「已反馈」的幂等键（同一个问题只标记一次）。
    让前端自己算哈希的话，就得在浏览器里再实现一套"归一化 + SHA-1"——两份实现
    迟早漂移，而漂移的表现是"明明反馈过了按钮还能点，点了又插一行"。
    """

    ok: bool = True
    message: str
    #: 问题归一化后的 SHA-1（40 位十六进制）
    q_hash: str
    #: True = 这次**更新**了防重窗口内已存在的那条反馈（👎 改 👍、补充原因）
    updated: bool = False


# --------------------------- 管理端 ---------------------------
class AdminUserOut(BaseModel):
    """管理端用户列表项（模块 3 的授权面板）。

    刻意**不含** ``password_hash``：这类"顺手把整行返回出去"是口令哈希泄露最常见的路径。
    """

    user_uuid: str
    username: str
    role: RoleName
    is_active: bool = True
    # MySQL DATETIME → pydantic 直接吃 datetime，序列化成 ISO 字符串给前端
    created_at: Optional[datetime] = None


class TopQuestionOut(BaseModel):
    """高频提问 / 检索无结果的问题（模块 6 的运营看板）。

    ``no_result_queries`` 复用同一个形状（多一个 ``count`` 的含义完全相同），
    所以不再定义一个只有名字不同的模型。
    """

    question: str
    count: int
    #: 最近一次被问的时间（列表按次数降序，这个字段用来判断"是新问题还是老问题"）
    last_at: Optional[datetime] = None


class TopDocumentOut(BaseModel):
    """被检索最多的文档。

    ``file_name`` 为空串 = 该文件**已被删除**：它确实被检索过，所以留在统计里，
    由前端显示占位文案（把删除的文件从历史统计里抹掉等于篡改数据）。
    """

    file_uuid: str
    file_name: str = ""
    hits: int


class FeedbackScopeStatOut(BaseModel):
    """按库分的好评/差评数；``scope`` 是原串，中文库名由前端 ``scopeLabel`` 翻。"""

    scope: str
    up: int = 0
    down: int = 0


class FeedbackReasonOut(BaseModel):
    reason: str
    count: int


class FeedbackSummaryOut(BaseModel):
    """反馈汇总。``up_rate`` 为 ``None`` = 还没有任何反馈（**不是** 0%）。"""

    total: int = 0
    up: int = 0
    down: int = 0
    up_rate: Optional[float] = None
    by_scope: List[FeedbackScopeStatOut] = []
    top_reasons: List[FeedbackReasonOut] = []


class AdminStatsOut(BaseModel):
    """运营看板（``GET /admin/stats``）：四个块 + 快照元信息。

    ``generated_at`` / ``window`` 一起回答"这些数字是什么时候、覆盖哪一段数据算出来的"——
    没有它们，管理员会把几分钟前的快照当成实时数，然后质疑"我刚点的差评怎么没上去"。
    """

    generated_at: str
    window: int
    retrieval_total: int = 0
    top_questions: List[TopQuestionOut] = []
    no_result_queries: List[TopQuestionOut] = []
    top_documents: List[TopDocumentOut] = []
    feedback_summary: FeedbackSummaryOut


class ServiceStatus(BaseModel):
    ok: bool
    detail: str


class HealthOut(BaseModel):
    es: ServiceStatus
    es_uses_ik: bool
    mysql: ServiceStatus
    redis: ServiceStatus
    chroma_blocks: int


class RagasEvalIn(BaseModel):
    scope: str = "public"
    limit: int = Field(5, ge=1, le=20, description="评测题目数量（会消耗 LLM 额度）")


class RagasEvalOut(BaseModel):
    scope: str
    sample_count: int
    judge_model: str
    ragas_version: str
    scores: dict
    metric_labels: dict
    rows: List[dict] = []
    report_path: Optional[str] = None
