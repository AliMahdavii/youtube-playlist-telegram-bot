# Fallback for hosts that deploy from a Dockerfile instead of a Procfile.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py config.py playlist.py tasks.py ./
CMD ["python", "bot.py"]
