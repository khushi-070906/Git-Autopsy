from app.analysis import static_python


def test_extracts_functions(demo_repo):
    results = static_python.analyze_repository_python_files(demo_repo)
    by_path = {r.path: r for r in results}
    assert "model_loader.py" in by_path
    fn_names = {fn.name for fn in by_path["model_loader.py"].functions}
    assert "tokenize" in fn_names
    assert "load_model" in fn_names


def test_never_executes_code(demo_repo):
    # A file that would raise/exit if executed must still be *parseable*
    # without side effects.
    bad_file = demo_repo / "dangerous.py"
    bad_file.write_text("import sys\nsys.exit(1)\nraise RuntimeError('should never run')\n")
    analysis = static_python.analyze_python_file(demo_repo, "dangerous.py")
    assert analysis.parse_error is None  # parsed fine
    assert "sys" in analysis.imports  # inspected via AST, not executed


def test_handles_syntax_errors_gracefully(demo_repo):
    bad_file = demo_repo / "broken.py"
    bad_file.write_text("def f(:\n    pass")
    analysis = static_python.analyze_python_file(demo_repo, "broken.py")
    assert analysis.parse_error is not None
