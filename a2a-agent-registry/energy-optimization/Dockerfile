# Build context must be set to a2a-agent-registry/ (parent dir) so that
# COPY can pick up both common/ and energy-optimization/.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY common/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY energy-optimization/ ./energy-optimization/

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app

EXPOSE 8080
CMD ["python", "energy-optimization/agent.py"]
