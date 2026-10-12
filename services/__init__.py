"""业务服务包（包初始化文件）。

注意：本文件原名称为 `_init_.py`（单下划线），属于拼写错误，已重命名为 `__init__.py`。
包初始化文件保持"零副作用"：不要在这里导入各 service 模块，
否则 `import services.es_service` 会连带把 docling/torch/streamlit 全部加载进来。
"""
