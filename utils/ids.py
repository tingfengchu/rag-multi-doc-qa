import re
import uuid

# 连续两个及以上下划线统一压成一个。
# 注意不能用 filename.replace("__", "_")：它是单次、非重叠替换，
# "____.pdf" 只会变成 "__.pdf"，仍然会污染 source 分隔符
# （实测会让 source.split("__") 切出 4 段，前端还原文件名就错位了）。
_MULTI_UNDERSCORE = re.compile(r"_{2,}")


def new_uuid() -> str:
    return str(uuid.uuid4())


def build_source(owner_uuid: str, file_uuid: str, filename: str) -> str:
    """
    唯一化 source 生成规则：
    owner_uuid + file_uuid + 原文件名

    这样即使用户名有特殊字符、重名、改名，都不会影响 source 唯一性。

    契约：返回值里 ``"__"`` 只会出现 2 次，且只作为分隔符，
    因此 ``source.split("__")`` 恒为 3 段：
        [owner_uuid, file_uuid, 原文件名]
    """
    safe_name = _MULTI_UNDERSCORE.sub("_", filename)  # 防止 __ 污染分隔符
    return f"{owner_uuid}__{file_uuid}__{safe_name}"
