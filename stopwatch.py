import time
from typing import Optional


class Stopwatch:
    def __init__(self) -> None:
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.running = False

    def start(self) -> None:
        if not self.running:
            self.start_time = time.perf_counter()
            self.end_time = None
            self.running = True

    def stop(self) -> None:
        if self.running:
            self.end_time = time.perf_counter()
            self.running = False

    def elapsed(self) -> float:
        if self.start_time is None:
            return 0.0
        if self.running:
            return time.perf_counter() - self.start_time
        if self.end_time is None:
            return 0.0
        return self.end_time - self.start_time

    def reset(self) -> None:
        self.start_time = None
        self.end_time = None
        self.running = False
