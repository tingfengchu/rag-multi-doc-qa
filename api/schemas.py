"""API 的请求/响应模型（Pydantic v2）。

约定：
* 字段名与前端 JSON 一致（snake_case，避免前端再写一层转换）；
* **不信任客户端传来的任何身份信息**——``user_uuid`` / ``role`` 一律来自会话，
  不出现在请求体里（注册也不允许指定 role，见 ``routers/auth.py``）；
* ``scope`` 由 ``deps.resolve_scope`` 校验，这里只做类型约束。
"""
from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

#: 与 Streamlit 侧一致的可选重排模型（中文语料建议 BGE）
RERANK_MODEL_CHOICES = (
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "BAAI/bge-reranker-base",
)

#: 用 Literal 而不是自由字符串：否则客户端能让我们去 HF 加载**任意**模型仓库，
#: 每个名字都对应一次真实下载 + 常驻内存，是典型的资源滥用入口。
RerankModel = Literal[
    "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "BAAI/bge-reranker-base",
]


# --------------------------- 认证 ---------------------------
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
    role: Literal["user", "admin"]


class MessageOut(BaseModel):
    ok: bool = True
    message: str


# --------------------------- 文件 ---------------------------
class FileOut(BaseModel):
    file_uuid: str
    file_name: str
    source: str


class UploadOut(BaseModel):
    ok: bool = True
    message: str
    file_uuid: Optional[str] = None
    duplicate: bool = False


# --------------------------- 问答 ---------------------------
class ChatIn(BaseModel):
    question: str = Field(..., min_length=1, description="用户问题")
    scope: str = Field("public", description="public 或 private:<user_uuid>")
    enable_rerank: bool = True
    rerank_model: RerankModel = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    file_uuid: Optional[str] = Field(None, description="限定检索单个文件；None = 全部")


class Citation(BaseModel):
    source: str
    page: int


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


# --------------------------- 管理端 ---------------------------
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
