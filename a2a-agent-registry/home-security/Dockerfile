FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY common/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY common/ ./common/
COPY home-security/ ./home-security/

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app

EXPOSE 8080
CMD ["python", "home-security/agent.py"]
