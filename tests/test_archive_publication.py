import os
import pytest
from scripts.publish_encrypted_archive import publish


def private_file(path, data=b"synthetic encrypted bytes"):
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def test_archive_no_clobber_and_durable_publication(tmp_path, monkeypatch):
    source = private_file(tmp_path / "temporary")
    target = tmp_path / "backup.gpg"
    calls = []
    actual = os.fsync

    def observe(fd):
        calls.append(os.fstat(fd).st_mode)
        actual(fd)

    monkeypatch.setattr(os, "fsync", observe)
    publish(source, target)
    assert len(calls) == 2
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        publish(private_file(tmp_path / "second", b"other"), target)
    assert target.read_bytes() == source.read_bytes()


def test_archive_rejects_unsafe_source_and_destination(tmp_path):
    target = tmp_path / "backup.gpg"
    empty = private_file(tmp_path / "empty", b"")
    with pytest.raises(ValueError):
        publish(empty, target)
    source = private_file(tmp_path / "source")
    source.chmod(0o644)
    with pytest.raises(ValueError):
        publish(source, target)
    source.chmod(0o600)
    alias = tmp_path / "alias"
    alias.symlink_to(source)
    with pytest.raises(OSError):
        publish(alias, target)
    target.symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        publish(source, target)
    assert target.is_symlink()
    assert not (tmp_path / "missing").exists()
