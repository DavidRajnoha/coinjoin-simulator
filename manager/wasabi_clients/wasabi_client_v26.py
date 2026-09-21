from time import sleep, time

from .wasabi_client_base import WasabiClientBase


class WasabiClientV26(WasabiClientBase):

    def __init__(
        self,
        host="localhost",
        port=37128,
        name="wasabi-client",
        proxy="",
        version="2.6.0",
        delay=(0, 0),
        stop=(0, 0),
        skip_rounds=(),
    ):
        super().__init__(host, port, name, proxy, version, delay, stop, skip_rounds)

    def wait_wallet(self, timeout=None):
        start = time()
        while timeout is None or time() - start < timeout:
            try:
                self._create_wallet()
            except Exception:
                pass

            try:
                if self.wallet_started(timeout=5):
                    return True
            except Exception:
                pass

            sleep(1)
        return False
