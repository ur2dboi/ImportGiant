FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=3000
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system --gid 10001 importgiant \
    && useradd --system --uid 10001 --gid importgiant --home-dir /app importgiant \
    && mkdir -p /data && chown 10001:10001 /data && chmod 0700 /data
COPY app.py migration.py ./
COPY public ./public
USER 10001:10001
EXPOSE 3000
CMD ["python", "app.py"]
