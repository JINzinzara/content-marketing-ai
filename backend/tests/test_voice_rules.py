from backend.text_ai import voice_rules, voice_stats

FEED = ["레몬 마들렌🍋\n\n상큼한 레몬 글레이즈드가 깔끔함을 더해줘요\n가장 많이 찾아주시는 메뉴에요ㅎㅎ\n\n#마들렌 #대구카페"] * 4


def test_counts_body_lines_only():
    stats = voice_stats(FEED)
    assert stats["lines"] == 12  # 해시태그 줄 제외
    assert stats["period"] == 0 and stats["formal"] == 0
    assert round(stats["emoji"], 2) == 0.33


def test_feed_without_periods_becomes_rules():
    rules = voice_rules(FEED)
    assert any("마침표를 쓰지 않는다" in r for r in rules)
    assert any("에요" in r for r in rules)
    assert any("이모지" in r for r in rules)


def test_formal_feed_keeps_periods_and_too_few_lines_gives_no_rules():
    formal = ["신메뉴입니다.\n많이 찾아주세요.\n감사합니다."] * 4
    rules = voice_rules(formal)
    assert any("마침표를 쓴다" in r for r in rules)
    assert not any("에요" in r for r in rules)
    assert voice_rules(["한 줄뿐이에요"]) == []
