ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache ffmpeg py3-pip python3
RUN python3 -m pip install --break-system-packages --no-cache-dir google-genai websockets

WORKDIR /app
COPY viper_core /app/viper_core
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
