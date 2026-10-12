"""FastAPI 应用层（Phase 1）。

与 Streamlit 版**并存**，共用同一套业务层（``services/`` / ``auth.py`` / ``utils/`` /
``config.py``），所以两边行为天然一致，也不会出现"改了一边忘了另一边"。

启动：
    .venv\\Scripts\\python.exe -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1
"""
