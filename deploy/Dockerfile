# Agent console, web side only: the page, status, password unlock and sessions.
# Unprivileged: no Docker socket, read-only filesystem. Actions go to the host
# runner over a Unix socket mounted from the run directory.
FROM python:3.13-alpine
# cryptography: verifies Touch ID / Face ID (passkey) signatures; everything else is standard library
RUN pip install --no-cache-dir "cryptography==43.0.3"
COPY agent-console.py /app/agent-console.py
USER 1000:1000
EXPOSE 8097
CMD ["python3", "-u", "/app/agent-console.py", "serve"]
