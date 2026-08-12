from fast_tokenizer import tokenize as _tokenize

def tokenize(text):
    # switched to new tokenizer library after dependency upgrade
    return _tokenize(text)

def load_model(path, cache=True):
    return {"path": path, "tokenizer": tokenize, "cache": cache}
