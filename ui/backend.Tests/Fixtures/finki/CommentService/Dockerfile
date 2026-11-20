FROM tiangolo/uvicorn-gunicorn-fastapi:python3.10

COPY requirements.txt .

RUN pip install -r requirements.txt

WORKDIR /app

COPY . .

RUN pip install fastapi uvicorn

RUN opentelemetry-bootstrap -a install

# One uvicorn worker is one process on one core, so a replica could never use
# more than a single core no matter how idle the host was. UVICORN_WORKERS
# lets a replica spread across cores; set it to 1 to get the old behaviour.
ENV UVICORN_WORKERS=4
CMD ["sh", "-c", "opentelemetry-instrument uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS}"]