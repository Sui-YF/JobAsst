FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_HEADLESS=true

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data/uploads /app/data/exports /app/logs \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app/data /app/logs

USER appuser
EXPOSE 8501
VOLUME ["/app/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').getenv('STREAMLIT_SERVER_PORT','8501') + '/_stcore/health', timeout=3)"

CMD ["sh", "-c", "python -m streamlit run app.py --server.address 0.0.0.0 --server.port ${STREAMLIT_SERVER_PORT:-8501} --server.headless true"]
