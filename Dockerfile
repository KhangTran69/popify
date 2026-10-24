FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps .
COPY app ./app
COPY migrations ./migrations
COPY workflows ./workflows
COPY scripts ./scripts
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "python -m scripts.migrate && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
