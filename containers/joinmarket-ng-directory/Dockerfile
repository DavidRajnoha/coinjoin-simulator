ARG JM_NG_TAG=0.39.2
FROM ghcr.io/joinmarket-ng/joinmarket-ng/directory-server:${JM_NG_TAG}

# Upstream image already runs as uid 1000 (user "jm").
USER 1000:1000

# Reference JoinMarket announces regtest as "testnet"; NG follows the same convention.
ENV NETWORK_CONFIG__NETWORK=testnet
ENV DIRECTORY_SERVER__HOST=0.0.0.0
ENV DIRECTORY_SERVER__PORT=5222
ENV DIRECTORY_SERVER__NICK_AUTH_DIRECTORY_ID=test:jm-directory-5222
ENV LOGGING__LEVEL=INFO

EXPOSE 5222 8080

CMD ["jm-directory-server"]
