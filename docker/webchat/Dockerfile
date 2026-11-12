FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
ARG DUCTOR_UID=1000
WORKDIR /app
COPY . /app/src
RUN pip install --no-cache-dir "/app/src[webchat]" \
    && useradd --create-home --uid "${DUCTOR_UID}" ductor
USER ductor
EXPOSE 8744
CMD ["python", "-m", "ductor_bot.webchat.web"]
