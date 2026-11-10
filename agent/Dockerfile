# ROVOps vehicle agent
#
# Installed as a BlueOS Extension.  The agent needs host networking so it can
# bind the local MAVLink UDP endpoint (default udpin:0.0.0.0:14599).
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENT_HEALTH_PORT=8081

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY rovops_agent ./rovops_agent

LABEL version="0.1.0"
LABEL type="extension"
LABEL authors="ROVOps"
LABEL company="ROVOps"
LABEL requirements="core >= 1.0"
LABEL permissions='{\
  "ExposedPorts": {"8081/tcp": {}}, \
  "HostConfig": {\
    "NetworkMode": "host", \
    "RestartPolicy": {"Name": "unless-stopped"}\
  }\
}'

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=3)"

CMD ["python", "-m", "rovops_agent.main"]
