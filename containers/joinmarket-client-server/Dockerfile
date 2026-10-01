FROM joinmarket-latest:yg-pe-rpc


# Copy the config file to the right place

# Starts the RPC server
# RUN python3 /jm/clientserver/scripts/jmwalletd.py

RUN apt-get update -qq && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        socat ca-certificates iproute2 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Onion messaging "testing mode" (regtest_count != 0,0) dials directory nodes over plain TCP but
# advertises the bot's location as 127.0.0.1:<port>, which the joinmarket-ng directory server
# rejects (it only accepts *.onion or NOT-SERVING-ONION). That address is unreachable to peers
# anyway, so advertise NOT-SERVING-ONION: all private messages are relayed via the directory.
RUN sed -i 's/self.onion_hostname = "127.0.0.1"/self.onion_hostname = NOT_SERVING_ONION_HOSTNAME/' \
        /jm/clientserver/src/jmdaemon/onionmc.py \
    && grep -q "self.onion_hostname = NOT_SERVING_ONION_HOSTNAME" /jm/clientserver/src/jmdaemon/onionmc.py

# When the maker chosen to broadcast does not, the taker pushes the transaction itself; if that
# also fails (typically a mempool conflict: a maker's coin was already spent in a concurrent
# coinjoin), handle_unbroadcast_transaction only logs "Failed to broadcast transaction" and never
# reports the attempt as finished. The daemon then stays "taker running" forever and a tumbler
# stops for the rest of the run (1-3 tumblers per 1000-block run in the 2025 campaign).
# Report the failure the way Taker.push() already does for an immediate self-push failure, so the
# tumbler takes its own "possible mempool conflict" path and retries the entry with new makers.
RUN python3 - <<'EOF'
p = "/jm/clientserver/src/jmclient/taker.py"
s = open(p).read()
old = '''                jlog.error("Failed to broadcast transaction: ")
                jlog.info(btc.human_readable_transaction(tx))
'''
assert s.count(old) == 1, "taker.py changed upstream; re-check the unbroadcast patch"
s = s.replace(old, old + "                self.on_finished_callback(False, fromtx=True)\n")
open(p, "w").write(s)
EOF

RUN useradd -ms /bin/sh joinmarket
USER 1000:1000

# Create the SSL directory and generate the SSL certificates
RUN mkdir -p /home/joinmarket/.joinmarket/ssl \
    && openssl req -newkey rsa:2048 -nodes -keyout /home/joinmarket/.joinmarket/ssl/key.pem \
    -x509 -days 365 -out /home/joinmarket/.joinmarket/ssl/cert.pem -subj "/C=US/ST=State/L=City/O=Organization/OU=Unit/CN=localhost"

# Generates the empty wallet, so we can place the config there
RUN python3 /jm/clientserver/scripts/wallet-tool.py generate; exit 0

COPY --chown=joinmarket:joinmarket run.sh /home/joinmarket/
# run.sh assembles ~/.joinmarket/joinmarket.cfg = base + messaging fragment at start
COPY --chown=joinmarket:joinmarket joinmarket.cfg /home/joinmarket/joinmarket.base.cfg
COPY --chown=joinmarket:joinmarket messaging-irc.cfg messaging-onion.cfg /home/joinmarket/
RUN chmod +x /home/joinmarket/run.sh
WORKDIR /home/joinmarket

ENV MODE=walletd
ENV JM_MESSAGING=irc
ENV JM_DIRECTORY_NODES=jm-directory:5222

CMD ["./run.sh"]
