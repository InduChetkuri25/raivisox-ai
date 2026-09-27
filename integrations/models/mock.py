"""Mock model provider for development and testing."""

import asyncio
import random
import time
from typing import Optional

from integrations.models.base import (
    BaseModelProvider,
    ProviderType,
    ModelCategory,
    ModelInfo,
    ChatRequest,
    ChatResponse,
    CompletionRequest,
    CompletionResponse,
    ImageRequest,
    ImageResponse,
)


class MockModelProvider(BaseModelProvider):
    """Mock model provider for development and testing without real API keys."""

    provider_type = ProviderType.MOCK

    # Mock models
    models = [
        ModelInfo(
            id="mock-reasoning",
            name="Mock Reasoning Model",
            provider=ProviderType.MOCK,
            category=ModelCategory.REASONING,
            context_window=128000,
            max_output_tokens=8192,
            quality_rating=0.8,
            latency_estimate_ms=500,
        ),
        ModelInfo(
            id="mock-coder",
            name="Mock Coding Model",
            provider=ProviderType.MOCK,
            category=ModelCategory.CODING,
            context_window=128000,
            max_output_tokens=8192,
            quality_rating=0.85,
            latency_estimate_ms=400,
        ),
        ModelInfo(
            id="mock-fast",
            name="Mock Fast Model",
            provider=ProviderType.MOCK,
            category=ModelCategory.REASONING,
            context_window=32000,
            max_output_tokens=4096,
            quality_rating=0.6,
            latency_estimate_ms=100,
        ),
        ModelInfo(
            id="mock-vision",
            name="Mock Vision Model",
            provider=ProviderType.MOCK,
            category=ModelCategory.VISION,
            context_window=128000,
            max_output_tokens=4096,
            supports_vision=True,
            quality_rating=0.75,
            latency_estimate_ms=600,
        ),
    ]

    # Mock responses for different scenarios
    mock_responses = {
        "greeting": [
            "Hello! I'm RAIVISOX AI, your adaptive digital intelligence assistant. How can I help you today?",
            "Hi there! Ready to turn your intent into verified outcomes. What would you like to accomplish?",
        ],
        "help": [
            "I can help you with various tasks including building applications, researching topics, analyzing data, and more. Just describe what you want to accomplish.",
            "Tell me what you'd like me to do. I can build applications, write code, analyze documents, and execute complex workflows.",
        ],
        "coding": [
            "I'll help you build that application. Let me start by understanding the requirements and creating a plan.",
            "Great! I'll create a plan for building this application, then generate the code, test it, and verify everything works.",
        ],
        "default": [
            "I understand. Let me process this request and provide you with the best outcome possible.",
            "I'll work on this for you. My goal is to deliver a verified, high-quality result.",
            "Understood. I'll analyze this request and execute the optimal strategy.",
        ],
    }

    def __init__(self, simulate_latency: bool = True, simulate_errors: bool = False):
        """Initialize the mock provider."""
        super().__init__()
        self.simulate_latency = simulate_latency
        self.simulate_errors = simulate_errors
        self.call_count = 0
        self.total_tokens_used = 0

    async def chat_complete(self, request: ChatRequest) -> ChatResponse:
        """Simulate a chat completion."""
        self.call_count += 1

        # Simulate latency
        if self.simulate_latency:
            model_info = self.get_model_info(request.model)
            latency = model_info.latency_estimate_ms if model_info else 500
            await asyncio.sleep(latency / 1000)

        # Simulate occasional errors
        if self.simulate_errors and random.random() < 0.1:
            return ChatResponse(
                content="",
                model=request.model,
                usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                finish_reason="error",
                error="Simulated API error for testing",
            )

        # Generate response based on context
        last_message = request.messages[-1].content.lower() if request.messages else ""

        if any(word in last_message for word in ["hello", "hi", "hey"]):
            response_text = random.choice(self.mock_responses["greeting"])
        elif any(word in last_message for word in ["help", "what can you"]):
            response_text = random.choice(self.mock_responses["help"])
        elif any(word in last_message for word in ["code", "build", "create", "app", "application"]):
            response_text = random.choice(self.mock_responses["coding"])
        else:
            response_text = random.choice(self.mock_responses["default"])

        # Add some variability
        if random.random() < 0.3 and request.messages:
            # Reference previous context
            prev = request.messages[-2].content if len(request.messages) > 1 else ""
            if prev:
                response_text = f"Regarding your previous message about '{prev[:50]}...': {response_text}"

        # Calculate mock token counts
        prompt_tokens = len(request.messages) * 50 + len(last_message) // 4
        completion_tokens = len(response_text) // 4
        total_tokens = prompt_tokens + completion_tokens

        self.total_tokens_used += total_tokens

        return ChatResponse(
            content=response_text,
            model=request.model,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
            finish_reason="stop",
        )

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Simulate a text completion."""
        self.call_count += 1

        if self.simulate_latency:
            await asyncio.sleep(0.1)

        # Generate completion based on prompt
        prompt = request.prompt.lower()

        if "def " in prompt or "function" in prompt:
            completion = "\n    # Implementation here\n    pass\n"
        elif "class " in prompt:
            completion = "\n    def __init__(self):\n        pass\n"
        else:
            completion = " " + " ".join(random.choices(
                ["the", "a", "is", "was", "has", "have", "can", "will", "should", "would"],
                k=random.randint(5, 15)
            ))

        prompt_tokens = len(prompt) // 4
        completion_tokens = len(completion) // 4

        return CompletionResponse(
            content=completion,
            model=request.model,
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            finish_reason="stop",
        )

    async def generate_image(self, request: ImageRequest) -> ImageResponse:
        """Simulate image generation."""
        await asyncio.sleep(0.5)  # Simulate generation time

        # Generate mock URLs
        mock_urls = [
            f"https://mock-image-service.example.com/generated/{random.randint(1000, 9999)}.png"
            for _ in range(request.n)
        ]

        return ImageResponse(
            urls=mock_urls,
            model=request.model,
        )
