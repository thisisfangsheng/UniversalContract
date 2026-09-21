FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /tmp/uc-requirements.txt
COPY demo/digital-workforce-platform/backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/uc-requirements.txt -r /tmp/requirements.txt
COPY . /app/UniversalContract
ENV PYTHONPATH=/app/UniversalContract/demo/digital-workforce-platform/backend
CMD ["uvicorn", "digital_workforce.main:app", "--host", "0.0.0.0", "--port", "8000"]
