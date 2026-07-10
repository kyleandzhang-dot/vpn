# 使用官方 Python 轻量级镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 复制当前目录下的所有文件到容器的 /app 里
COPY . /app

# 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 暴露 FastAPI 运行的 8000 端口
EXPOSE 8000

# 启动命令 (假设你的主程序叫 main.py，并且实例叫 app)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]