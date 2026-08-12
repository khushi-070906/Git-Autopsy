from app.analysis.dependency_parser import (
    parse_go_mod,
    parse_package_json,
    parse_pyproject_toml,
    parse_requirements_txt,
)


def test_parse_requirements_txt():
    text = "requests==2.28.0\n# comment\nnumpy>=1.20\n-e git+https://example.com/x\n"
    deps = parse_requirements_txt(text)
    names = {d["name"] for d in deps}
    assert names == {"requests", "numpy"}


def test_parse_package_json():
    text = '{"dependencies": {"react": "^18.0.0"}, "devDependencies": {"vite": "^5.0.0"}}'
    deps = parse_package_json(text)
    names = {d["name"] for d in deps}
    assert names == {"react", "vite"}


def test_parse_pyproject_toml():
    text = '[project]\nname = "x"\ndependencies = ["requests>=2.0", "click"]\n'
    deps = parse_pyproject_toml(text)
    names = {d["name"] for d in deps}
    assert "requests" in names
    assert "click" in names


def test_parse_go_mod():
    text = "module example.com/x\n\ngo 1.21\n\nrequire (\n\tgithub.com/foo/bar v1.2.3\n)\n"
    deps = parse_go_mod(text)
    names = {d["name"] for d in deps}
    assert "github.com/foo/bar" in names


def test_malformed_input_never_raises():
    assert parse_package_json("not json") == []
    assert parse_pyproject_toml("not = valid = toml = [") == []
