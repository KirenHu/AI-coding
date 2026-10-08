FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY worktwin ./worktwin
COPY third_party ./third_party
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 worktwin && mkdir /data && chown worktwin /data
USER worktwin
ENV WORKTWIN_SERVER_DATA_DIR=/data
EXPOSE 8789
CMD ["python", "-m", "uvicorn", "worktwin.server:create_server", "--factory", "--host", "0.0.0.0", "--port", "8789", "--no-access-log"]
