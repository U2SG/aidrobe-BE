from app.infrastructure.storage.source_image import key_from_url


def test_plain_internal_url() -> None:
    assert key_from_url("http://localhost:9000/aidrobe/users/u/a.jpg", "aidrobe") == "users/u/a.jpg"


def test_signed_url_drops_the_signature() -> None:
    # Purchase screenshots are passed as presigned GET URLs; the query string
    # must not end up in the object key.
    url = "http://localhost:9000/aidrobe/users/u/buy%20me.jpg?X-Amz-Signature=abc&X-Amz-Expires=900"
    assert key_from_url(url, "aidrobe") == "users/u/buy me.jpg"


def test_external_url_is_not_ours() -> None:
    assert key_from_url("https://shop.example.com/img/a.jpg", "aidrobe") is None
