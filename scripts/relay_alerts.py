"""Deliver durable monitor events to an explicitly selected local receipt directory.

No network connection or external notification is made. A sink must return
only after durable acceptance; an exception leaves the event pending.
"""
import argparse
import json
import os
import re
import tempfile
from pathlib import Path

from scripts.check_operations import update_outbox, private_directory, private_regular


class LocalReceiptSink:
    def __init__(self, directory):
        self.directory = Path(directory)
        private_directory(self.directory)

    def __call__(self, event):
        event_id = event.get("event_id", "")
        if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{32}", event_id):
            raise ValueError("Invalid event ID")
        data = (json.dumps(event, sort_keys=True, ensure_ascii=True) + "\n").encode()
        if len(data) > 16384:
            raise ValueError("Invalid receipt size")
        destination = self.directory / (event_id + ".json")
        temp = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".receipt-", delete=False) as handle:
                temp = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, destination, follow_symlinks=False)
            except FileExistsError:
                # Replay is accepted only for the same immutable receipt.
                fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as existing:
                    private_regular(existing.fileno())
                    if existing.read(len(data) + 1) != data:
                        raise ValueError("Conflicting receipt")
                    os.fsync(existing.fileno())
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temp is not None:
                os.unlink(temp)


def relay_pending(outbox, sink, *, limit=100):
    if not 1 <= limit <= 500:
        raise ValueError("Invalid relay limit")
    events = update_outbox(outbox)
    delivered = 0
    for event in events[:limit]:
        sink(event)
        update_outbox(outbox, acknowledge=event["event_id"])
        delivered += 1
    return delivered


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outbox-file", required=True)
    parser.add_argument("--receipt-dir", required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        delivered = relay_pending(args.outbox_file, LocalReceiptSink(args.receipt_dir), limit=args.limit)
        remaining = len(update_outbox(args.outbox_file))
    except (OSError, ValueError):
        print(json.dumps({"status": "unavailable", "detail": "Local receipt delivery failed; inspect pending events and receipt directory."}))
        return 2
    print(json.dumps({"status": "pending" if remaining else "clear", "delivered": delivered, "pending_count": remaining}))
    return 1 if remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())
