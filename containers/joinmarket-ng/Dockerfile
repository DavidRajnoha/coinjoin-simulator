ARG JM_NG_TAG=0.39.2
FROM ghcr.io/joinmarket-ng/joinmarket-ng/jmwalletd:${JM_NG_TAG}

# The upstream image runs as root with data in /root; the emulator runs pods as uid 1000.
RUN useradd -m -u 1000 -s /bin/sh joinmarket
USER 1000:1000
WORKDIR /home/joinmarket

COPY --chown=joinmarket:joinmarket run.sh /home/joinmarket/run.sh
RUN chmod +x /home/joinmarket/run.sh

ENV JOINMARKET_DATA_DIR=/home/joinmarket/.joinmarket-ng
ENV JMWALLETD_HOST=0.0.0.0

EXPOSE 28183

CMD ["./run.sh"]
