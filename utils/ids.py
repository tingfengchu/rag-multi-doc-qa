import uuid

def new_uuid() -> str:
    return str(uuid.uuid4())

def build_source(owner_uuid: str, file_uuid: str, filename: str) -> str:
    """
    唯一化 source 生成规则：
    owner_uuid + file_uuid + 原文件名
    这样即使用户名有特殊字符、重名、改名，都不会影响 source 唯一性。
    """
    safe_name = filename.replace("__", "_")  # 防止 __ 污染分隔符
    return f"{owner_uuid}__{file_uuid}__{safe_name}"