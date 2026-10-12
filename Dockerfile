FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m unittest discover -s tests -q
ENV LAB_DATA=/app/data
EXPOSE 5008
CMD ["gunicorn", "--bind", "0.0.0.0:5008", "--workers", "1", "--threads", "16", "--timeout", "60", "--access-logfile", "-", "app:app"]
