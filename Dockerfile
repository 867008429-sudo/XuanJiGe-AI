FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据库持久化目录
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/xuanjige.db

ENV DEEPSEEK_API_KEY=""
ENV PORT=8888

EXPOSE 8888

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://127.0.0.1:8888/health', timeout=3)" || exit 1

# gevent worker 支持 SSE 流式输出
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8888", \
     "--workers", "2", \
     "--worker-class", "gevent", \
     "--worker-connections", "100", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "app:app"]
