FROM python:3.13-slim

# git is needed to pip-install the druidforms framework from its git pin
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY lamahub /app/lamahub
COPY pyproject.toml /app/pyproject.toml
COPY requirements.txt /app/requirements.txt

RUN pip install /app

WORKDIR /app

CMD [ "uvicorn", "lamahub.app:app", "--host", "0.0.0.0", "--port", "5000" ]
