FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# The code is an importable package, which keeps relative module imports explicit.
COPY *.py ./pulse/
COPY static ./pulse/static
# Run without root and allow writes only to the mounted SQLite data directory.
RUN addgroup --system pulse && adduser --system --ingroup pulse pulse \
    && mkdir -p /data && chown -R pulse:pulse /app /data
USER pulse
ENV PYTHONUNBUFFERED=1
# Import the package entry point; app.py handles HTTP wiring and worker lifecycle.
CMD ["uvicorn", "pulse.app:app", "--host", "0.0.0.0", "--port", "8000"]
