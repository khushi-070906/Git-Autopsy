import subprocess
from pathlib import Path

import pytest


def _run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def demo_repo(tmp_path: Path) -> Path:
    """Builds a 3-commit repo: working -> unrelated change -> dependency-driven regression."""
    repo = tmp_path / "demo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "dev@example.com"], repo)
    _run(["git", "config", "user.name", "Dev"], repo)

    (repo / "model_loader.py").write_text(
        "def tokenize(text):\n    return text.split(' ')\n\n"
        "def load_model(path):\n    return {'path': path, 'tokenizer': tokenize}\n"
    )
    (repo / "requirements.txt").write_text("requests==2.28.0\n")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_model_loader.py").write_text(
        "from model_loader import tokenize\n\n"
        "def test_tokenize_basic():\n    assert tokenize('hello world') == ['hello', 'world']\n"
    )
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "Initial working version with basic tokenizer"], repo)

    (repo / "model_loader.py").write_text(
        "def tokenize(text):\n    return text.split(' ')\n\n"
        "def load_model(path, cache=True):\n    return {'path': path, 'tokenizer': tokenize, 'cache': cache}\n"
    )
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "Add caching option to load_model"], repo)

    (repo / "requirements.txt").write_text("requests==2.31.0\nfast-tokenizer==2.0.0\n")
    (repo / "model_loader.py").write_text(
        "from fast_tokenizer import tokenize as _tokenize\n\n"
        "def tokenize(text):\n    return _tokenize(text)\n\n"
        "def load_model(path, cache=True):\n    return {'path': path, 'tokenizer': tokenize, 'cache': cache}\n"
    )
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "Upgrade dependency versions and switch to fast-tokenizer"], repo)

    return repo
