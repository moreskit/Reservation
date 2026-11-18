FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 JOSHY_DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY joshy ./joshy
RUN pip install --no-cache-dir .
VOLUME /data
EXPOSE 8000
CMD ["joshy", "serve", "--host", "0.0.0.0", "--port", "8000"]
