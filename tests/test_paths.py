from pathlib import Path

from voice import APP_NAME, paths


def test_config_dir_uses_xdg_and_app_name(isolated_xdg):
    d = paths.config_dir()
    assert d == isolated_xdg / "xdg_config_home" / APP_NAME
    assert d.is_dir()


def test_all_paths_are_namespaced(isolated_xdg):
    assert paths.config_file().name == "config.toml"
    assert paths.history_file().parent == isolated_xdg / "xdg_state_home" / APP_NAME
    assert paths.portal_token_file().parent == paths.state_dir()
    assert paths.cache_dir() == isolated_xdg / "xdg_cache_home" / APP_NAME
    assert paths.socket_path() == isolated_xdg / "xdg_runtime_dir" / f"{APP_NAME}.sock"


def test_runtime_dir_falls_back_to_tmp_when_unset(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert paths.runtime_dir() == Path("/tmp")
