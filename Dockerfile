FROM node:22-alpine AS web-builder

WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/telegram-priority

RUN groupadd --gid 10001 telegram-priority \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin telegram-priority

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 --from=web-builder /build/app/web_static ./app/web_static

RUN mkdir -p /database /session \
    && chown -R 10001:10001 /srv/telegram-priority /database /session

USER 10001:10001

CMD ["python", "-m", "app.main"]
