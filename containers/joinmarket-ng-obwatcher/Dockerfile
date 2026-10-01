ARG JM_NG_TAG=0.39.2
FROM ghcr.io/joinmarket-ng/joinmarket-ng/orderbook-watcher:${JM_NG_TAG}

# Upstream image already runs as uid 1000 (user "jm").
USER 1000:1000

# DIRECTORY_NODES and NETWORK_CONFIG__NICK_AUTH_DIRECTORY_IDS are supplied by the engine at run time.
ENV NETWORK_CONFIG__NETWORK=testnet
ENV NETWORK_CONFIG__ALLOW_CLEARNET_CONNECTIONS=true
ENV ORDERBOOK_WATCHER__HTTP_HOST=0.0.0.0
ENV ORDERBOOK_WATCHER__HTTP_PORT=8000
ENV LOGGING__LEVEL=INFO

EXPOSE 8000

CMD ["python", "-m", "orderbook_watcher.main"]
