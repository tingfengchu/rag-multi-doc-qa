# 使用官方 Python 3.12 镜像，完美避开 SQLite 版本问题
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量，让 HuggingFace 走国内镜像
ENV HF_ENDPOINT=https://hf-mirror.com
ENV PYTHONUNBUFFERED=1

# 先复制依赖文件并安装，利用 Docker 缓存层，加快构建速度
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制项目所有代码
COPY . .

# 暴露 Streamlit 默认端口
EXPOSE 8501

# 启动命令
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0"]