import json
import logging
import threading
import time
from collections import Counter

LOGGER = logging.getLogger("team_memory.metrics")
LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1, 5, 30, 120)
ROUTE_CLASSES = (("/dashboard/api/", "dashboard_api"), ("/dashboard", "dashboard_page"), ("/mcp", "mcp"),
                 ("/api/call", "api_call"), ("/healthz", "healthz"), ("/learning", "learning"))


def route_class(path: str) -> str:
    if path == "/":
        return "index"
    return next((name for prefix, name in ROUTE_CLASSES if path.startswith(prefix)), "other")


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.counters: Counter = Counter()
        self.histogram: Counter = Counter()
        self.latency_sum: Counter = Counter()

    def count(self, name: str, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self.lock:
            self.counters[key] += 1
        LOGGER.info(json.dumps({"event": name, **labels}))

    def observe(self, route: str, method: str, status: int, seconds: float) -> None:
        with self.lock:
            self.counters[("http_requests_total", (("method", method), ("route", route), ("status", str(status))))] += 1
            for bound in LATENCY_BUCKETS:
                if seconds <= bound:
                    self.histogram[(route, str(bound))] += 1
            self.histogram[(route, "+Inf")] += 1
            self.latency_sum[route] += seconds
        LOGGER.info(json.dumps({"event": "http_request", "route": route, "method": method,
                                "status": status, "duration_seconds": round(seconds, 4)}))

    def snapshot(self) -> dict:
        with self.lock:
            counters = [{"name": name, "labels": dict(labels), "value": value}
                        for (name, labels), value in sorted(self.counters.items())]
            histogram = [{"route": route, "le": bound, "count": value}
                         for (route, bound), value in sorted(self.histogram.items())]
            sums = dict(self.latency_sum)
        return {"counters": counters, "latency_buckets": histogram, "latency_sum_seconds": sums}


class RequestMetrics:
    def __init__(self, app, metrics: Metrics):
        self.app, self.metrics = app, metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = {"code": 500}

        async def observed_send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            self.metrics.observe(route_class(scope["path"]), scope["method"], status["code"], time.monotonic() - started)
