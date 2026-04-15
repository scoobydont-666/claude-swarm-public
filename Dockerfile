FROM python:3.12-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
RUN uv pip install --system --no-cache fastapi uvicorn httpx
COPY src/ ./src/

FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app
ENV PYTHONPATH=/app/src
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; r = httpx.get('http://127.0.0.1:8000/health'); r.raise_for_status()"
CMD ["uvicorn", "dashboard:app", "--host", "0.0.0.0", "--port", "8000"]
