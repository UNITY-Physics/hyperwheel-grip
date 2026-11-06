import os
import stat

import pytest

from hyperwheel.keys import Credentials, KeyMissing, KeyStore, KeyUnreadable


def test_write_and_read_back_with_mode_600(tmp_path):
    store = KeyStore(tmp_path)
    path = store.write("StudyX-SiteB", Credentials("client-1", "very-secret"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert store.read("StudyX-SiteB") == Credentials("client-1", "very-secret")


def test_secret_not_in_repr():
    assert "very-secret" not in repr(Credentials("client-1", "very-secret"))


def test_missing_and_insecure_keys(tmp_path):
    store = KeyStore(tmp_path)
    with pytest.raises(KeyMissing):
        store.read("Nope")
    path = store.write("Open", Credentials("c", "s"))
    os.chmod(path, 0o644)
    with pytest.raises(KeyUnreadable, match="mode 600"):
        store.read("Open")
    with pytest.raises(KeyUnreadable):
        store.read("../etc")


def test_service_reads_the_systemd_copy(tmp_path, monkeypatch):
    KeyStore(tmp_path, prefix="keys_").write("A", Credentials("c", "s"))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert KeyStore.for_process(tmp_path / "unused").read("A").client_id == "c"
    monkeypatch.delenv("CREDENTIALS_DIRECTORY")
    assert KeyStore.for_process(tmp_path).directory == tmp_path


def test_bad_contents_never_echo_the_file(tmp_path):
    p = tmp_path / "A.json"
    fd = os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, b'{"client_id": "c", "secret": "leaky"}')
    os.close(fd)
    with pytest.raises(KeyUnreadable) as e:
        KeyStore(tmp_path).read("A")
    assert "leaky" not in str(e.value)
