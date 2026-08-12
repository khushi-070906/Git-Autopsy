import pytest

from app.security import InvalidRepositoryURL, safe_join, validate_github_url


def test_accepts_plain_github_url():
    assert validate_github_url("https://github.com/psf/requests") == "https://github.com/psf/requests.git"


def test_accepts_dot_git_suffix():
    assert validate_github_url("https://github.com/psf/requests.git") == "https://github.com/psf/requests.git"


@pytest.mark.parametrize("bad_url", [
    "https://evil.com/psf/requests",
    "git@github.com:psf/requests.git",
    "file:///etc/passwd",
    "https://github.com/psf/requests; rm -rf /",
    "https://github.com/psf/../../etc/passwd",
    "https://github.com/psf/requests`whoami`",
    "ext::sh -c 'touch pwned'",
    "",
    "   ",
])
def test_rejects_malicious_or_invalid_urls(bad_url):
    with pytest.raises(InvalidRepositoryURL):
        validate_github_url(bad_url)


def test_safe_join_blocks_traversal(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(ValueError):
        safe_join(base, "..", "..", "etc", "passwd")


def test_safe_join_allows_normal_paths(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    result = safe_join(base, "src", "file.py")
    assert str(result).startswith(str(base))
