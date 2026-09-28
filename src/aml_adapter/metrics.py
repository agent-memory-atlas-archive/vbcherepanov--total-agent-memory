"""In-process request metrics (counter + histogram) in Prometheus text format.

The adapter exposes no metrics endpoint — AML allows only health/add/search
on the public listener — so the registry is written periodically to
`<data_dir>/metrics.prom` for a node_exporter textfile collector, and each
request is also logged as one JSON line.
"""

from __future__ import annotations

import os
import threading
from collections import Counter
from pathlib import Path

DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 1800.0)


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: Counter[tuple[str, str]] = Counter()
        self.buckets: Counter[tuple[str, float]] = Counter()
        self.duration_sum: Counter[str] = Counter()
        self.duration_count: Counter[str] = Counter()
        self.items: Counter[str] = Counter()

    def observe(self, operation: str, status: str, seconds: float) -> None:
        with self.lock:
            self.requests[(operation, status)] += 1
            self.duration_sum[operation] += seconds
            self.duration_count[operation] += 1
            for bound in DURATION_BUCKETS:
                if seconds <= bound:
                    self.buckets[(operation, bound)] += 1

    def count_items(self, name: str, amount: int) -> None:
        with self.lock:
            self.items[name] += amount

    def render(self) -> str:
        with self.lock:
            lines = [
                "# HELP aml_requests_total AML adapter requests by operation and outcome.",
                "# TYPE aml_requests_total counter",
            ]
            lines += [f'aml_requests_total{{operation="{op}",status="{status}"}} {value}'
                      for (op, status), value in sorted(self.requests.items())]
            lines += [
                "# HELP aml_request_duration_seconds AML adapter request latency.",
                "# TYPE aml_request_duration_seconds histogram",
            ]
            for op in sorted(self.duration_count):
                for bound in DURATION_BUCKETS:
                    lines.append(f'aml_request_duration_seconds_bucket{{operation="{op}",le="{bound:g}"}} '
                                 f'{self.buckets[(op, bound)]}')
                lines.append(f'aml_request_duration_seconds_bucket{{operation="{op}",le="+Inf"}} '
                             f'{self.duration_count[op]}')
                lines.append(f'aml_request_duration_seconds_sum{{operation="{op}"}} {self.duration_sum[op]:.6f}')
                lines.append(f'aml_request_duration_seconds_count{{operation="{op}"}} {self.duration_count[op]}')
            lines += [
                "# HELP aml_items_total Fragments stored and search results returned.",
                "# TYPE aml_items_total counter",
            ]
            lines += [f'aml_items_total{{kind="{name}"}} {value}' for name, value in sorted(self.items.items())]
        return "\n".join(lines) + "\n"

    def write_textfile(self, path: Path) -> None:
        temporary = path.with_suffix(".prom.tmp")
        temporary.write_text(self.render(), encoding="utf-8")
        os.replace(temporary, path)
