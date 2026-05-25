# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Системные библиотеки.
# - libpq5: runtime для psycopg2-binary (build-essential НЕ нужен)
# - postgresql-client: pg_dump для бэкапов
# - tzdata: тайм-зоны
# - libfreetype/libpng/fonts: matplotlib без X-сервера
# - curl: для healthcheck
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpq5 \
        tzdata \
        postgresql-client \
        libfreetype6 \
        libpng16-16 \
        fonts-dejavu-core \
        curl \
    && ln -snf /usr/share/zoneinfo/Europe/Moscow /etc/localtime \
    && echo "Europe/Moscow" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Непривилегированный пользователь
RUN groupadd --system --gid 1001 bot && \
    useradd --system --uid 1001 --gid bot --home /app --shell /sbin/nologin bot

WORKDIR /app

# Зависимости (отдельный слой для кэша)
COPY --chown=bot:bot requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Исходники
COPY --chown=bot:bot . .

# Каталог для бэкапов и matplotlib-кэша под нашим пользователем
RUN mkdir -p /backup /app/.config/matplotlib && \
    chown -R bot:bot /backup /app/.config

USER bot

# Простой healthcheck: процесс жив + питон отвечает
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1

CMD ["python", "-m", "bot.main"]
