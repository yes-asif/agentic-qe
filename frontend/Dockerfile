FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir streamlit==1.38.0 streamlit-autorefresh==1.0.1 \
    websocket-client==1.8.0 pandas==2.2.2 plotly==5.24.0 requests==2.32.3

COPY frontend ./frontend

ENV PYTHONUNBUFFERED=1
EXPOSE 8501

CMD ["streamlit", "run", "frontend/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
