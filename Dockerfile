FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# 成片合成用 FFmpeg；硬字幕与示例素材需要中文字体。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY comicdrama ./comicdrama
RUN python -m pip install --no-cache-dir --no-deps . \
    && useradd --uid 10001 --create-home app && mkdir /state && chown app:app /state
USER 10001:10001
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health/live', timeout=3)"
CMD ["python", "-m", "comicdrama"]
