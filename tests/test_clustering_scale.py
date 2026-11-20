"""The rare-keyword candidate generation of `best_match` must give exactly the clusters of an exhaustive scan."""

import random
import time
from collections import Counter, defaultdict

from app.analysis import Article, best_match, keyword_overlap


def exhaustive_best_match(keys, seeds, index, min_shared=3, min_overlap=0.5):
    """The previous implementation: score every seed that shares ANY word (ties: earliest seed)."""
    if len(keys) < 2:
        return None
    best, best_score = None, 0.0
    for idx, shared in sorted(Counter(i for k in keys for i in index.get(k, ())).items()):
        overlap = keyword_overlap(shared, len(keys), len(seeds[idx]), min_shared, min_overlap)
        if overlap > best_score:
            best, best_score = idx, overlap
    return best


def greedy(articles, matcher, **kw):
    stories: list[list[Article]] = []
    seeds: list[set[str]] = []
    index: dict[str, list[int]] = defaultdict(list)
    for art in articles:
        hit = matcher(art.keys, seeds, index, **kw)
        if hit is None:
            stories.append([art])
            seeds.append(art.keys)
            for k in art.keys:
                index[k].append(len(stories) - 1)
        elif all(a.source_name != art.source_name for a in stories[hit]):
            stories[hit].append(art)
    return [[a.url for a in s] for s in stories]


def synthetic(n, seed=7, vocab=900, events=250):
    """Zipf-like vocabulary (a few very common words) and events that several outlets cover with variations."""
    rnd = random.Random(seed)
    words = [f"w{i}" for i in range(vocab)]
    weights = [1 / (i + 1) for i in range(vocab)]
    topics = [rnd.sample(words[40:], 7) for _ in range(events)]
    out = []
    for i in range(n):
        if rnd.random() < 0.6:
            base = rnd.choice(topics)
            keys = {w for w in base if rnd.random() < 0.8} | set(rnd.choices(words[:40], weights[:40], k=rnd.randint(0, 2)))
        else:
            keys = set(rnd.choices(words, weights, k=rnd.randint(2, 9)))
        out.append(Article(title=" ".join(sorted(keys)), url=f"u{i}", summary="", published=i, media=None,
                           source_name=f"outlet{rnd.randrange(40)}", keys=keys))
    return out


def test_new_candidates_give_identical_clusters():
    arts = synthetic(3000)
    for kw in ({}, {"min_shared": 2, "min_overlap": 0.4}, {"min_shared": 4, "min_overlap": 0.6}):
        old = greedy(arts, exhaustive_best_match, **kw)
        new = greedy(arts, best_match, **kw)
        assert new == old
        assert 100 < len(new) < len(arts)  # the data really produces merged and separate stories


def test_identical_for_many_random_seeds_and_tiny_headlines():
    for seed in range(8):
        arts = synthetic(400, seed=seed, vocab=60, events=20)  # tiny vocabulary: lots of ties and overlaps
        assert greedy(arts, best_match) == greedy(arts, exhaustive_best_match)


def test_clustering_speed_bound():
    arts = synthetic(6000, vocab=3000, events=800)
    started = time.perf_counter()
    greedy(arts, best_match)
    assert time.perf_counter() - started < 8  # about 0.3 s on a laptop; the bound only catches an accidental O(n^2)
