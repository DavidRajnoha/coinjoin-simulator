# Bitcoin Core 25: the newest series that still creates legacy (BDB) wallets for the
# reference JoinMarket client without -deprecatedrpc, while providing the RPC
# arguments joinmarket-ng needs (listsinceblock include_change, gettxspendingprevout).
FROM lncm/bitcoind:v25.1
USER root
RUN apk add --no-cache curl jq \
    && mkdir -p /home/bitcoin/.bitcoin /home/bitcoin/data \
    && chown -R 100:101 /home/bitcoin
COPY --chown=100:101 bitcoin.conf /home/bitcoin/.bitcoin/bitcoin.conf
COPY --chown=100:101 mine.sh /home/bitcoin/mine.sh
COPY --chown=100:101 run.sh /home/bitcoin/run.sh
USER 100:101
WORKDIR /home/bitcoin
ENTRYPOINT []
CMD ["./run.sh"]
