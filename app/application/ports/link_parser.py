from typing import Protocol


class LinkParserPort(Protocol):
    async def extract_image_url(self, source_url: str) -> str | None: ...
