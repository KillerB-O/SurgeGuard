FROM python:3.12-slim

WORKDIR /app

COPY simulator/pyproject.toml /app/simulator/pyproject.toml
RUN pip install --no-cache-dir "fastapi>=0.135" "httpx>=0.27" "pydantic>=2.13" "uvicorn[standard]>=0.32"

COPY simulator /app/simulator

EXPOSE 8010

CMD ["uvicorn", "simulator.app:app", "--host", "0.0.0.0", "--port", "8010"]
