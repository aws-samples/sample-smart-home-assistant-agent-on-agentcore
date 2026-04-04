FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# AgentCore Runtime requires port 8080
EXPOSE 8080

# Run the agent app
CMD ["python", "agent.py"]
