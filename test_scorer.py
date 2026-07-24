from app.ranking.scorer import _extract_json, flatten_ai_result


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_markdown_fence():
    text = "```json\n{\"a\": 1, \"b\": 2}\n```"
    assert _extract_json(text) == {"a": 1, "b": 2}


def test_extract_json_with_preamble_text():
    text = 'Sure, here is the JSON: {"overall_score": 88}'
    assert _extract_json(text) == {"overall_score": 88}


def test_extract_json_invalid_returns_none():
    assert _extract_json("not json at all") is None


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
    merged = flatten_ai_result(video, ai_result)
    assert merged["video_id"] == "abc123"
    assert merged["overall_score"] == 91.5
    assert merged["difficulty"] == "intermediate"
    assert merged["_is_educational"] is True
