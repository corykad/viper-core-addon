ARG BUILD_FROM=python:3.13-slim
FROM ${BUILD_FROM}

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m pip install --no-cache-dir google-genai websockets==15.0.1 aiortc==1.14.0 Pillow==11.3.0

WORKDIR /app
COPY viper_core /app/viper_core

CMD ["python3", "-m", "viper_core"]
