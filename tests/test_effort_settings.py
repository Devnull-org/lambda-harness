"""Effort preferences extend legacy settings without changing task/model isolation."""

import sqlite3

from lambda_harness.project_settings import ProjectSettings


def test_effort_settings_preserve_legacy_models_and_persist_per_task(tmp_path):
    with sqlite3.connect(tmp_path / "project-settings.sqlite") as connection:
        connection.execute(
            "CREATE TABLE task_models (task_id TEXT PRIMARY KEY, backend TEXT NOT NULL, model TEXT)"
        )
        connection.execute("INSERT INTO task_models VALUES ('one', 'codex', 'selected')")
    with ProjectSettings(tmp_path) as settings:
        assert settings.task_model("one") == {"backend": "codex", "model": "selected"}
        settings.save_task_model("one", "codex", "selected", "high")
        settings.save_task_model("two", "codex", "other", "low")
    with ProjectSettings(tmp_path) as settings:
        assert settings.task_model("one")["effort"] == "high"
        assert settings.task_model("two")["effort"] == "low"
        settings.save_task_model("one", "codex", "selected", None)
        assert "effort" not in settings.task_model("one")
        assert settings.task_model("two")["effort"] == "low"
