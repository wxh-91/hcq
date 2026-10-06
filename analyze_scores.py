def analyze_scores(scores):
    if not scores:
        return None

    result = {
        '优秀': 0,
        '良好': 0,
        '及格': 0,
        '不及格': 0,
    }

    for s in scores:
        if 90 <= s <= 100:
            result['优秀'] += 1
        elif 80 <= s <= 89:
            result['良好'] += 1
        elif 60 <= s <= 79:
            result['及格'] += 1
        else:
            result['不及格'] += 1

    result['最高分'] = max(scores)
    result['最低分'] = min(scores)

    return result
