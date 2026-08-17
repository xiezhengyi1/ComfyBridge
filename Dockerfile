# ComfyBridge 云部署镜像
# 构建：docker build -t comfybridge .
# 运行：见 README「云上部署」；密钥用环境变量传入，勿把 config.json 打进公共镜像
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py comfy_client.py config.py job_manager.py prompt_enhance.py safety.py workflow_engine.py worker_pool.py ./
COPY static/ ./static/
COPY workflows/ ./workflows/
COPY blocklist.json ./blocklist.json

# config.json 不入镜像（含密钥），用环境变量注入；首次启动会自动生成 API Key 并打印到日志
ENV COMFYBRIDGE_HOST=0.0.0.0 \
    COMFYBRIDGE_PORT=8000

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
