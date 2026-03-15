ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /app

RUN apk add --no-cache python3 py3-aiohttp

COPY app /app
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD ["/run.sh"]
