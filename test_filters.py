from app.youtube.filters import passes_category_filter, passes_keyword_filter


def test_rejects_reaction_video():
    assert not passes_keyword_filter(
        "Reacting to the WORST Python code ever", "watch me react live"
    )


def test_accepts_python_tutorial():
    assert passes_keyword_filter(
        "Build a RAG pipeline with Ollama - Full Python Tutorial",
        "step by step guide to building a local retrieval augmented generation app",
    )


def test_rejects_unrelated_positive_free_text():
    # Has none of the accept keywords -> rejected even without a reject keyword
    assert not passes_keyword_filter("My morning routine vlog", "just chatting today")


def test_category_filter_blocks_news():
    assert not passes_category_filter("25")  # News & Politics


def test_category_filter_allows_education():
    assert passes_category_filter("27")  # Education
    assert passes_category_filter(None)
