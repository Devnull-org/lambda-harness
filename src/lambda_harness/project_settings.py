"""Project identity and preferences, separate from shared backend definitions."""

import sqlite3
from pathlib import Path

from .workspace import digest


def project_identity(repository):
    repository = Path(repository).expanduser().resolve()
    return {
        "id": digest(str(repository))[:16],
        "name": repository.name or str(repository),
        "repository": str(repository),
    }


class ProjectSettings:
    def __init__(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(directory / "project-settings.sqlite", timeout=30)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS settings "
            "(singleton INTEGER PRIMARY KEY CHECK(singleton = 1), backend TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS task_models "
            "(task_id TEXT PRIMARY KEY, backend TEXT NOT NULL, model TEXT)"
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()

    def backend(self):
        row = self.connection.execute("SELECT backend FROM settings WHERE singleton = 1").fetchone()
        return row[0] if row else None

    def save_backend(self, backend):
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO settings VALUES (1, ?)", (backend,))

    def task_model(self, task_id):
        row = self.connection.execute(
            "SELECT backend, model FROM task_models WHERE task_id = ?", (task_id,)
        ).fetchone()
        return {"backend": row[0], "model": row[1]} if row else None

    def save_task_model(self, task_id, backend, model):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO task_models VALUES (?, ?, ?)", (task_id, backend, model)
            )
