# 星载参数库 MVCC 可串行化冻结审计 —— 运行镜像
# 仅依赖 Python 标准库，保持镜像精简
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/audits.db

WORKDIR /opt/service

# 先拷依赖清单（本服务零三方依赖），再拷源码，利用构建缓存
COPY app/ ./app/
COPY tests/ ./tests/
COPY verify.py ./verify.py

RUN mkdir -p /data && python -m py_compile app/*.py verify.py

EXPOSE 8080

# 容器级健康检查：命中真实接口 /healthz
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8080')+'/healthz',timeout=3); assert json.loads(r.read())['status']=='ok'" || exit 1

CMD ["python", "-m", "app.server"]
