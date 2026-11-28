"""Synchronize and publish a private ciphertext file without replacing a backup."""
import os
import stat
import sys
from pathlib import Path


def publish(source, destination):
    source, destination = Path(source), Path(destination)
    if source.parent.resolve() != destination.parent.resolve():
        raise ValueError("Archive publication must use the same directory")
    with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), "rb") as archive:
        info = os.fstat(archive.fileno())
        if not stat.S_ISREG(info.st_mode) or not info.st_size or info.st_mode & 0o077:
            raise ValueError("Archive must be a nonempty private regular file")
        os.fsync(archive.fileno())
    os.link(source, destination, follow_symlinks=False)
    directory = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: publish_encrypted_archive.py PRIVATE_TEMP OUTPUT")
    try:
        publish(*sys.argv[1:])
    except (OSError, ValueError):
        raise SystemExit("Archive publication failed; inspect the private destination before retrying.")
