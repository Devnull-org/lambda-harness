"""Backend choices are data, and explicit overrides have predictable precedence."""

import pytest

from lambda_harness.backends import Backend
from lambda_harness.config import Route, RoutedAgent, Settings, load_settings


def test_custom_profile_requires_no_provider_code(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'default_backend = "hosted"\n[backends.hosted]\n'
        'command = ["team-model-bridge"]\ninput_format = "json"\n'
    )
    settings = load_settings(path)
    assert settings.default_backend == "hosted"
    assert settings.backends["hosted"].command == ["team-model-bridge"]


def test_profile_replacement_does_not_inherit_other_provider_arguments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[backends.local]\ncommand = ["team-model-bridge"]\ninput_format = "json"\n')
    backend = load_settings(path).backends["local"]
    assert backend.response_fields == []
    assert backend.unset_env == []
    assert backend.probe == []


def test_default_role_and_cli_overrides():
    settings = Settings(
        default_backend="one",
        backends={
            "one": Backend(command=["one"], model="default-one"),
            "two": Backend(command=["two"], model="default-two"),
        },
        roles={"security_reviewer": Route(backend="two", model="careful")},
    )
    agent = RoutedAgent(settings)
    assert agent.resolve("implementer").name == "one"
    assert agent.resolve("implementer").model == "default-one"
    assert agent.resolve("security_reviewer").name == "two"
    assert agent.resolve("security_reviewer").model == "careful"
    assert RoutedAgent(settings, model="override").resolve("security_reviewer").model == "override"
    selected = RoutedAgent(settings, backend="one").resolve("security_reviewer")
    assert (selected.name, selected.model) == ("one", "default-one")


def test_unknown_backend_is_not_silently_replaced():
    with pytest.raises(ValueError, match="Unknown backend"):
        RoutedAgent(load_settings(), backend="typo")


@pytest.mark.parametrize(
    "extra",
    [
        'default_backend = "missing"',
        '[roles.typo]\nbackend = "local"',
        '[roles.implementer]\nbackend = "missing"',
        "[backends.bad]\ncommand = []",
        '[backends.bad]\ncommand = ["tool"]\ntimeout = -1',
        '[backends.bad]\ncommand = ["tool"]\ntimeuot = 10',
    ],
)
def test_invalid_configuration_fails_early(tmp_path, extra):
    path = tmp_path / "invalid.toml"
    path.write_text(extra)
    with pytest.raises(ValueError):
        load_settings(path)


def test_missing_explicit_config_is_not_ignored(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "missing.toml")


def test_default_configuration_does_not_require_existing_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "not-created"))
    assert load_settings().default_backend == "auto"
    assert not (tmp_path / "not-created").exists()


def test_repository_config_is_never_automatically_executed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    (tmp_path / "config.toml").write_text('default_backend = "malicious"')
    assert load_settings().default_backend == "auto"


def test_model_arguments_must_forward_the_selected_model():
    with pytest.raises(ValueError, match="model_args"):
        Backend(command=["tool"], model_args=["--model", "fixed-model"])


def test_task_timeout_reaches_selected_agent_without_leaking_to_other_tasks(monkeypatch, tmp_path):
    from lambda_harness.backends import CommandAgent

    seen = []

    def invoke(self, role, context, cwd):
        seen.append((self.timeout, self.model, self.effort))
        return {"answer": "Done"}

    monkeypatch.setattr(CommandAgent, "__call__", invoke)
    settings = Settings(default_backend="one", backends={"one": Backend(command=["one"])})
    agent = RoutedAgent(settings, model="chosen", effort="high")
    agent("interviewer", {"agent_timeout": 1800}, tmp_path)
    agent("interviewer", {}, tmp_path)
    assert seen == [(1800, "chosen", "high"), (600, "chosen", "high")]
