"""减灌试验配对：按生效批次的各群归属概率，找最相近的一对同品种地块。"""

from .domain import Profile

PROFILE_ORDER = tuple(Profile)


def membership_vector(assignment):
    """地块在生效批次中的各群归属概率向量；旧批次缺向量时退化为所属群的one-hot。"""
    probs = assignment.get('probabilities') or {assignment['profile']: 1.0}
    return tuple(float(probs.get(profile, 0.0)) for profile in PROFILE_ORDER)


def closest_pair(candidates):
    """同品种候选中归属概率欧氏距离最小的一对。返回 (对照, 减量, 距离)，对照为编号较小者。"""
    candidates = sorted(candidates, key=lambda item: item['code'])
    best = None
    for index, first in enumerate(candidates):
        for second in candidates[index + 1:]:
            if first['cultivar'] != second['cultivar']:
                continue
            distance = sum((x - y) ** 2 for x, y in zip(first['vector'], second['vector']))
            key = (distance, first['code'], second['code'])
            if best is None or key < best[0]:
                best = (key, first, second)
    if best is None:
        return None
    return best[1], best[2], best[0][0]
