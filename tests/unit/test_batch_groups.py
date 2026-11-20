import uuid

from app.application.import_tasks.batch_groups import (
    BatchCandidate,
    GarmentImage,
    UnionFind,
    best_member,
    color_family,
    garment_kind,
    group_candidates,
)
from app.application.import_tasks.import_batch import CachedJudge
from app.infrastructure.vision.fake_vision import FakeVision

PHOTOS = [uuid.uuid4() for _ in range(4)]


def candidate(
    photo: int, garment: str, *, category: str = "top", color: str = "white", name: str = "t_shirt",
    image: bool = True, hidden: bool = False, confidence: float = 0.8, item: uuid.UUID | None = None,
) -> BatchCandidate:
    return BatchCandidate(
        task_id=PHOTOS[photo], temp_item_id=f"{garment}-{photo}", category=category, color_main=color,
        title=name, canonical_name=name,
        image=GarmentImage(key=None, url=f"https://cdn/{garment}/{photo}.jpg") if image else None,
        confidence=confidence, partially_hidden=hidden, confirmed_item_id=item,
    )


async def _load(image: GarmentImage) -> bytes:
    # https://cdn/<garment>/<photo>.jpg -> the same garment in every photo.
    assert image.url is not None
    return f"garment:{image.url.split('/')[3]} {image.url}".encode()


def judge(budget: int = 100) -> CachedJudge:
    return CachedJudge({}, FakeVision(), _load, budget=budget)


def keys(groups: list[list[BatchCandidate]]) -> list[list[str]]:
    return [[c.temp_item_id for c in group] for group in groups]


def test_color_family_merges_neighbouring_shades() -> None:
    assert color_family("light blue") == color_family("navy") == "blue"
    assert color_family("Cream") == color_family("off-white") == "white"
    assert color_family("grey") == "gray"
    assert color_family("teal") == "teal"


def test_garment_kind_prefers_the_canonical_name() -> None:
    assert garment_kind("t_shirt", "白色针织T恤") == "tee"
    assert garment_kind("long_sleeve_top", "米白色针织上衣") is None
    assert garment_kind(None, "黄色毛衣") == "knit"


def test_union_find_joins_transitively() -> None:
    uf = UnionFind(["a", "b", "c"])
    uf.union("a", "b")
    uf.union("c", "b")
    assert uf.find("c") == uf.find("b") == uf.find("a") == "c"


async def test_same_garment_across_photos_merges_and_others_stay_apart() -> None:
    candidates = [
        candidate(0, "tee"), candidate(0, "jeans", category="bottom", color="blue", name="jeans"),
        candidate(1, "tee"), candidate(1, "jeans", category="bottom", color="light_blue", name="jeans"),
        candidate(2, "other-tee"),
    ]
    grouping = await group_candidates(candidates, judge())
    assert keys(grouping.groups) == [["tee-0", "tee-1"], ["jeans-0", "jeans-1"], ["other-tee-2"]]
    assert grouping.pending is False


async def test_two_garments_of_one_photo_never_merge() -> None:
    # Same marker (look-alike) in one photo: still two garments.
    candidates = [candidate(0, "tee"), BatchCandidate(**{**candidate(0, "tee").__dict__, "temp_item_id": "tee-b"})]
    grouping = await group_candidates(candidates, judge())
    assert len(grouping.groups) == 2


async def test_bucket_and_type_gate_comparisons() -> None:
    seen: list[str] = []

    async def spy(a: GarmentImage, b: GarmentImage, garment: str, **options: object) -> bool:
        seen.append(garment)
        return True

    candidates = [
        candidate(0, "x"), candidate(1, "x", color="black"),  # other colour family
        candidate(2, "x", category="bottom"),  # other category
        candidate(3, "x", name="shirt"),  # tee vs shirt
    ]
    grouping = await group_candidates(candidates, spy)
    assert seen == []
    assert len(grouping.groups) == 4


async def test_hidden_or_uncropped_candidates_stay_on_their_own() -> None:
    candidates = [candidate(0, "tee"), candidate(1, "tee", hidden=True), candidate(2, "tee", image=False)]
    grouping = await group_candidates(candidates, judge())
    assert len(grouping.groups) == 3


async def test_a_candidate_matching_two_groups_bridges_them() -> None:
    calls: list[tuple[str | None, str | None]] = []

    async def verdicts(a: GarmentImage, b: GarmentImage, garment: str, **options: object) -> bool:
        calls.append((a.url, b.url))
        # The founders (photos 0 and 1) were judged different; photo 2 matches both.
        return "/2.jpg" in (a.url or "") + (b.url or "")

    candidates = [candidate(0, "a"), candidate(1, "b"), candidate(2, "c")]
    grouping = await group_candidates(candidates, verdicts)
    assert keys(grouping.groups) == [["a-0", "b-1", "c-2"]]


async def test_budget_leaves_comparisons_pending() -> None:
    candidates = [candidate(i, "tee") for i in range(3)]
    grouping = await group_candidates(candidates, judge(budget=0))
    assert grouping.pending is True
    assert len(grouping.groups) == 3


async def test_cached_verdicts_are_reused_without_vision() -> None:
    cache = {("https://cdn/a/0.jpg", "https://cdn/b/1.jpg"): True}
    cached = CachedJudge(cache, FakeVision(), _load, budget=0)
    grouping = await group_candidates([candidate(0, "a"), candidate(1, "b")], cached)
    assert keys(grouping.groups) == [["a-0", "b-1"]]
    assert cached.new == {}


async def test_members_confirmed_to_one_item_stay_together() -> None:
    item = uuid.uuid4()
    candidates = [candidate(0, "a", item=item), candidate(1, "b", image=False, item=item)]
    grouping = await group_candidates(candidates, judge(budget=0))
    assert keys(grouping.groups) == [["a-0", "b-1"]]


def test_best_member_prefers_a_mergeable_confident_crop() -> None:
    members = [candidate(0, "a", image=False, confidence=0.99), candidate(1, "a", confidence=0.6),
               candidate(2, "a", confidence=0.9)]
    assert best_member(members).temp_item_id == "a-2"


class _OneWayVision(FakeVision):
    """Says "same" only when asked in one image order: a single, unconfirmed yes."""

    def __init__(self) -> None:
        self.asked: list[tuple[bytes, bytes]] = []

    async def same_garment(self, crop_a: bytes, crop_b: bytes, garment: str) -> bool:
        self.asked.append((crop_a, crop_b))
        return crop_a > crop_b


async def test_shoes_need_two_yes_answers_but_tops_one() -> None:
    vision = _OneWayVision()
    shoes = [candidate(0, "x", category="shoes", name="sneakers"), candidate(1, "y", category="shoes", name="sneakers")]
    tops = [candidate(2, "p"), candidate(3, "q")]
    strict = CachedJudge({}, vision, _load, budget=10)
    grouping = await group_candidates(shoes + tops, strict)
    assert keys(grouping.groups) == [["x-0"], ["y-1"], ["p-2", "q-3"]]
    # Shoes: asked both ways (1 yes + 1 no); tops: asked once.
    assert len(vision.asked) == 3
    shoe_pair = next(pair for pair in strict.new if "/x/" in pair[0])
    assert strict.new[shoe_pair] is False  # the combined verdict is what gets cached


async def test_a_strict_comparison_needs_budget_for_both_calls() -> None:
    shoes = [candidate(0, "x", category="shoes", name="sneakers"), candidate(1, "x", category="shoes", name="sneakers")]
    short = CachedJudge({}, FakeVision(), _load, budget=1)
    grouping = await group_candidates(shoes, short)
    assert len(grouping.groups) == 2 and grouping.pending and short.exhausted and short.calls == 0
    enough = CachedJudge({}, FakeVision(), _load, budget=2)
    assert len((await group_candidates(shoes, enough)).groups) == 1
    assert enough.calls == 2


async def test_local_prescreen_answers_without_the_model() -> None:
    vision = _OneWayVision()
    screened: list[bool] = []

    def screen(a: bytes, b: bytes, same_photo: bool) -> bool | None:
        screened.append(same_photo)
        return False

    candidates = [candidate(0, "a"), candidate(1, "a")]
    local = CachedJudge({}, vision, _load, budget=10, screen=screen)
    grouping = await group_candidates(candidates, local)
    assert len(grouping.groups) == 2
    assert vision.asked == [] and local.calls == 0 and local.screened == 1 and screened == [False]
    assert list(local.new.values()) == [False]  # cached like a model verdict


async def test_cache_only_judge_records_missing_pairs() -> None:
    cache_only = CachedJudge({})
    grouping = await group_candidates([candidate(0, "a"), candidate(1, "a")], cache_only)
    assert grouping.pending and cache_only.missing == {("https://cdn/a/0.jpg", "https://cdn/a/1.jpg")}
