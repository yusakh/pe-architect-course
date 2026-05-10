FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rollout_operator.py .

RUN addgroup --gid 1000 appgroup && adduser --uid 1000 --gid 1000 --no-create-home --disabled-password appuser
USER 1000

CMD ["python", "rollout_operator.py"]
