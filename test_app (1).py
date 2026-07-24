"""Unit tests for app.py -- run with: pytest test_app.py"""
import app as m


def test_rejects_reaction_video():
    assert not m.passes_keyword_filter("Reacting to the WORST Python code ever", "watch me react live")


def test_accepts_python_tutorial():
    assert m.passes_keyword_filter(
        "Build a RAG pipeline with Ollama - Full Python Tutorial",
        "step by step guide to building a local retrieval augmented generation app",
    )


def test_rejects_unrelated_positive_free_text():
    assert not m.passes_keyword_filter("My morning routine vlog", "just chatting today")


def test_category_filter_blocks_news():
    assert not m.passes_category_filter("News & Politics")


def test_category_filter_allows_education():
    assert m.passes_category_filter("Education")
    assert m.passes_category_filter("Science & Technology")
    assert m.passes_category_filter(None)


def test_youtube_client_normalize_maps_invidious_fields():
    item = {
        "videoId": "abc123",
        "title": "Build a RAG app",
        "description": "A tutorial",
        "author": "AI Dev Tips",
        "published": 1750000000,
        "genre": "Education",
        "keywords": ["rag", "python"],
        "videoThumbnails": [{"quality": "high", "url": "https://example.com/thumb.jpg"}],
        "viewCount": 1000,
        "likeCount": 50,
        "lengthSeconds": 754,
    }
    normalized = m.YouTubeClient.normalize(item, "rag tutorial")
    assert normalized["video_id"] == "abc123"
    assert normalized["channel_title"] == "AI Dev Tips"
    assert normalized["category_id"] == "Education"
    assert normalized["duration_seconds"] == 754
    assert normalized["thumbnail_url"] == "https://example.com/thumb.jpg"


def test_extract_json_plain():
    assert m._extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_markdown_fence():
    text = "```json\n{\"a\": 1, \"b\": 2}\n```"
    assert m._extract_json(text) == {"a": 1, "b": 2}


def test_extract_json_with_preamble_text():
    text = 'Sure, here is the JSON: {"overall_score": 88}'
    assert m._extract_json(text) == {"overall_score": 88}


def test_extract_json_invalid_returns_none():
    assert m._extract_json("not json at all") is None


def test_flatten_ai_result():
    video = {"video_id": "abc123", "title": "Test"}
    ai_result = {
        "overall_score": 91.5,
        "summary": "Teaches X",
        "skills_learned": ["Python", "Docker"],
        "github_repos_mentioned": ["https://github.com/example/repo"],
        "difficulty": "intermediate",
        "scores": {"educational_value": 9},
        "why_selected": "Great practical example",
        "is_educational": True,
    }
    merged = m.flatten_ai_result(video, ai_result)
    assert merged["video_id"] == "abc123"
    assert merged["overall_score"] == 91.5
    assert merged["difficulty"] == "intermediate"
    assert merged["_is_educational"] is True


def test_format_digest_renders_medal_and_link():
    videos = [{
        "video_id": "abc123", "title": "Build X",
        "channel_title": "Chan", "duration_seconds": 600, "difficulty": "beginner",
        "overall_score": 80, "summary": "Summary.", "_why_selected": "Because.",
        "skills_learned": ["Python"], "github_repos": [],
    }]
    text = m.format_digest(videos)
    assert "🥇" in text
    assert "abc123" in text
