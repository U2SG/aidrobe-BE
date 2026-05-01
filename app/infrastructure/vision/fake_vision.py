from app.application.ports.vision import VisionResult


class FakeVision:
    async def analyze_image(self, image_url: str) -> VisionResult:
        if "fail" in image_url:
            raise ValueError("fake vision failure")
        if "pants" in image_url or "bottom" in image_url:
            return VisionResult(
                title="Black trousers",
                category="bottom",
                color_main="black",
                confidence_score=0.85,
            )
        return VisionResult(
            title="White shirt",
            category="top",
            color_main="white",
            confidence_score=0.9,
        )
