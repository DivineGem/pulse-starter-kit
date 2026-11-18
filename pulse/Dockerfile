FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static ./static
RUN addgroup --system pulse && adduser --system --ingroup pulse pulse \
    && mkdir -p /data && chown -R pulse:pulse /app /data
USER pulse
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
