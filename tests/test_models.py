"""Where a model lives, and whether the copy here is the current one."""
import pytest

from voice.models import (UP_TO_DATE, UPDATE_AVAILABLE, NOT_DOWNLOADED, UNKNOWN,
                          hub_directory, local_revision, update_status)


def _cached(root, repo, sha):
    """A model cache directory the way huggingface_hub lays one out."""
    d = root / f"models--{repo.replace('/', '--')}" / "refs"
    d.mkdir(parents=True)
    (d / "main").write_text(sha)


def test_the_local_revision_is_read_from_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    _cached(tmp_path / "hub", "Systran/faster-whisper-medium", "abc123")
    assert local_revision("medium", None) == "abc123"


def test_a_model_that_is_not_here_has_no_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    assert local_revision("medium", None) is None


def test_a_matching_revision_is_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    _cached(tmp_path / "hub", "Systran/faster-whisper-medium", "same")
    monkeypatch.setattr("voice.models.remote_revision", lambda model: "same")
    assert update_status([("medium", None)]) == [("medium", UP_TO_DATE, "same")]


def test_a_different_revision_is_an_update(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    _cached(tmp_path / "hub", "Systran/faster-whisper-medium", "old")
    monkeypatch.setattr("voice.models.remote_revision", lambda model: "new")
    assert update_status([("medium", None)]) == [("medium", UPDATE_AVAILABLE, "new")]


def test_a_model_not_downloaded_yet_says_so(tmp_path, monkeypatch):
    """Not an update: it has never been here, and the first dictation fetches it."""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setattr("voice.models.remote_revision", lambda model: "new")
    assert update_status([("medium", None)]) == [("medium", NOT_DOWNLOADED, "new")]


def test_no_network_is_reported_rather_than_guessed(tmp_path, monkeypatch):
    """"could not check" is a different answer from "up to date", and saying
    the second when the first is true is how a stale model goes unnoticed."""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    _cached(tmp_path / "hub", "Systran/faster-whisper-medium", "old")

    def offline(model):
        raise OSError("no network")

    monkeypatch.setattr("voice.models.remote_revision", offline)
    assert update_status([("medium", None)]) == [("medium", UNKNOWN, None)]


def test_checking_never_touches_the_model_files(tmp_path, monkeypatch):
    """The whole point of a check is that it downloads nothing."""
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    _cached(tmp_path / "hub", "Systran/faster-whisper-medium", "old")
    monkeypatch.setattr("voice.models.remote_revision", lambda model: "new")
    before = sorted(p.name for p in (tmp_path / "hub").rglob("*"))
    update_status([("medium", None)])
    assert sorted(p.name for p in (tmp_path / "hub").rglob("*")) == before


def test_a_per_profile_folder_is_looked_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "unused"))
    _cached(tmp_path / "mine", "Systran/faster-whisper-medium", "here")
    assert local_revision("medium", tmp_path / "mine") == "here"
    assert hub_directory("medium", tmp_path / "mine").parent == tmp_path / "mine"
