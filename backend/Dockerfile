FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV PYTHONUNBUFFERED=1

# Run as a non-root user. This module (app/analysis/counterfactual.py)
# executes test suites from arbitrary cloned repos — running as root
# means a malicious test file could write/read anywhere in the
# container's filesystem it can reach. A dedicated unprivileged user
# limits that to whatever /app's ownership allows.
RUN useradd --create-home --uid 10001 autopsy \
    && chown -R autopsy:autopsy /app
USER autopsy

EXPOSE 8000
CMD uvicorn app.main:app --host 0.0.0.0 --port 8000
