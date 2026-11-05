"""Per-thread request trace. Providers append one record per HTTP request; the scheduler reads
them to learn latency, bytes and status (AutoThrottle, traffic accounting, incident evidence)
and which network exit was used. Nothing here touches the network."""

from contextlib import contextmanager
import threading

_local = threading.local()


@contextmanager
def collect():
    """Collect every request made in this thread inside the block."""
    previous = getattr(_local, "records", None)
    records = []
    _local.records = records
    try:
        yield records
    finally:
        _local.records = previous


def add(domain, url, status, latency_s, nbytes, egress, content_type=None):
    records = getattr(_local, "records", None)
    if records is not None:
        records.append(
            {
                "domain": domain,
                "url": url,
                "status": status,
                "latency_s": latency_s,
                "bytes": nbytes,
                "egress": egress,
                "content_type": content_type,
            }
        )
