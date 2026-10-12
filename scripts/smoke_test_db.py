"""数据库冒烟测试：验证 users / file_dedup / feedback 三张表可正常读写。

用法（项目根目录下）：
    python scripts/smoke_test_db.py

测试数据会自行清理（结束时会打印各表剩余行数）。
"""
import json
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from auth import authenticate, create_user
from db import get_conn
from services.feedback_service import get_bad_cases, save_feedback
from services.file_state import (
    check_duplicate, create_pending, delete_record, list_success_files,
    mark_failed, mark_success,
)

USERNAME = "verify_smoke_user"
PASSWORD = "Verify_123!"
results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" -> {detail}" if detail else ""))


# --- users ---
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE username = %s", (USERNAME,))
ok, msg = create_user(USERNAME, PASSWORD, role="user")
check("create_user", ok, str(msg))
info = authenticate(USERNAME, PASSWORD)
check("authenticate (good password)", bool(info) and info.get("username") == USERNAME, str(info))
check("authenticate (bad password rejected)", authenticate(USERNAME, "wrong-password") is None)
check("authenticate (unknown user rejected)", authenticate("no_such_user_xyz", PASSWORD) is None)

# --- file_dedup ---
# 🚨 这里必须用一个**独立的随机 scope**，不能写死 "public"！
#    旧版本写死 SCOPE = "public"，收尾清理时执行
#        DELETE FROM file_dedup WHERE scope = 'public'
#    会把真实的公共库文件记录**全部删光**：向量库和 ES 里的数据还在，
#    但前端"文件管理"变空、重复上传检测失效（本项目当前的 file_dedup
#    为空、而 Chroma 里还有 170 个块，就是这个原因造成的）。
HASH = "a" * 64
SCOPE = f"smoke:{uuid.uuid4().hex[:8]}"
OWNER = "u" * 36
FILE_UUID = "f" * 36
check("check_duplicate (empty)", check_duplicate(HASH, SCOPE) is None)
rec_id = create_pending(HASH, SCOPE, "demo.pdf", f"{OWNER}__{FILE_UUID}__demo.pdf", OWNER, FILE_UUID)
check("create_pending returns id", isinstance(rec_id, int) and rec_id > 0, str(rec_id))
check("check_duplicate (pending ignored)", check_duplicate(HASH, SCOPE) is None)
mark_success(FILE_UUID, SCOPE)
dup = check_duplicate(HASH, SCOPE)
check("check_duplicate (after success)", bool(dup) and dup["source"].endswith("demo.pdf"), str(dup))
files = list_success_files(SCOPE)
check("list_success_files", any(f["file_uuid"] == FILE_UUID for f in files), str(len(files)))
mark_failed(FILE_UUID, SCOPE)
check("mark_failed removes from success list", check_duplicate(HASH, SCOPE) is None)
create_pending(HASH, SCOPE, "demo.pdf", "s", OWNER, FILE_UUID)  # 先清理 failed 记录
delete_record(FILE_UUID, SCOPE)
check("delete_record", check_duplicate(HASH, SCOPE) is None)

# --- feedback ---
ok = save_feedback(
    user_uuid=OWNER, scope=SCOPE, question="什么是RAG？",
    rewritten_question="什么是检索增强生成？", answer="RAG 是检索增强生成……",
    citations=[{"file_name": "demo.pdf", "score": 0.87, "source": "s"}],
    rating="down", reason="回答不完整", file_uuid_filter=FILE_UUID,
)
check("save_feedback", ok is True)
bad = get_bad_cases(5)
check("get_bad_cases returns row", any(r["reason"] == "回答不完整" for r in bad), str(len(bad)))
check(
    "citations is valid JSON",
    any("demo.pdf" in json.dumps(r["citations"] or "", ensure_ascii=False) for r in bad),
)

# --- cleanup ---
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE username = %s", (USERNAME,))
        # 双重保险：即使 SCOPE 将来被改错，也只删本次测试创建的 file_uuid
        cur.execute(
            "DELETE FROM file_dedup WHERE scope = %s AND file_uuid = %s",
            (SCOPE, FILE_UUID),
        )
        cur.execute("DELETE FROM feedback WHERE user_uuid = %s", (OWNER,))
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT COUNT(*) FROM users), (SELECT COUNT(*) FROM file_dedup), "
            "(SELECT COUNT(*) FROM feedback)"
        )
        print("rows left (users, file_dedup, feedback):", cur.fetchone())

failed = [r for r in results if not r[1]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
print("ALL_OK" if not failed else "SOME_FAILED")
sys.exit(1 if failed else 0)
