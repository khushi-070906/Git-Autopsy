from model_loader import tokenize

def test_tokenize_basic():
    assert tokenize("hello world") == ["hello", "world"]
