import time
import uuid


class Timing:
    def __init__(self, conn, generation, *, voice=False):
        self.id = uuid.uuid4().hex
        self.generation = generation
        self.voice = voice
        self.started = time.monotonic()
        self.logger = conn.logger

    def mark(self, stage, since=None):
        elapsed = round((time.monotonic() - (self.started if since is None else since)) * 1000)
        self.logger.info(f'turn_latency id={self.id} stage={stage} ms={elapsed}')
