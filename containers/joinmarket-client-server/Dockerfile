FROM joinmarket-latest:yg-pe-rpc


# Copy the config file to the right place

# Starts the RPC server
# RUN python3 /jm/clientserver/scripts/jmwalletd.py

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        socat ca-certificates iproute2 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN useradd -ms /bin/sh joinmarket
USER 1000:1000

# Create the SSL directory and generate the SSL certificates
RUN mkdir -p /home/joinmarket/.joinmarket/ssl \
    && openssl req -newkey rsa:2048 -nodes -keyout /home/joinmarket/.joinmarket/ssl/key.pem \
    -x509 -days 365 -out /home/joinmarket/.joinmarket/ssl/cert.pem -subj "/C=US/ST=State/L=City/O=Organization/OU=Unit/CN=localhost"

# Generates the empty wallet, so we can place the config there
RUN python3 /jm/clientserver/scripts/wallet-tool.py generate; exit 0

COPY --chown=joinmarket:joinmarket run.sh /home/joinmarket/
COPY --chown=joinmarket:joinmarket joinmarket.cfg /home/joinmarket/.joinmarket/
RUN chmod +x /home/joinmarket/run.sh
WORKDIR /home/joinmarket

ENV MODE=walletd

CMD ["./run.sh"]
