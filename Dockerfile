FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY edge_engine ./edge_engine
RUN pip install --no-cache-dir ".[fast]"
# The database lives on a mounted volume so it survives redeploys.
ENV EDGE_DB=/data/edge.db PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8080
COPY deploy/start.sh /start.sh
RUN chmod +x /start.sh
CMD ["/start.sh"]
