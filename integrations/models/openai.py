"""OpenAI API adapter."""

from typing import Optional

from integrations.models.base import (
    BaseModelProvider,
    ProviderType,
    ModelCategory,
    ModelInfo,
    ChatRequest,
    ChatResponse,
    ImageRequest,
    ImageResponse,
)

try:
    import openai
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class OpenAIAdapter(BaseModelProvider):
    """OpenAI API adapter."""

    provider_type = ProviderType.OPENAI

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        """Initialize the OpenAI adapter."""
        super().__init__()
        self.api_key = api_key
        self.base_url = base_url or "https://api.openai.com/v1"

        if not OPENAI_AVAILABLE:
            raise ImportError(
                "openai package not installed. Install with: pip install openai"
            )

        if api_key:
            self.client = openai.AsyncOpenAI(api_key=api_key, base_url=self.base_url)
            self._check_available()
        else:
            self.client = None
            self._mark_unavailable("No API key provided")

    def _check_available(self):
        """Check if the provider is available."""
        # Will be checked when making requests
        pass

    def _mark_unavailable(self, reason: str):
        """Mark all models as unavailable."""
        for model in self.models:
            model.available = False
        self._unavailable_reason = reason

    @property
    def unavailable_reason(self) -> Optional[str]:
        return getattr(self, '_unavailable_reason', None)

    # Model definitions
    models = [
        # GPT-4 models
        ModelInfo(
            id="gpt-4-turbo",
            name="GPT-4 Turbo",
            provider=ProviderType.OPENAI,
            category=ModelCategory.REASONING,
            context_window=128000,
            max_output_tokens=4096,
            supports_streaming=True,
            supports_functions=True,
            supports_vision=True,
            quality_rating=0.95,
            latency_estimate_ms=2000,
            cost_per_1k_tokens=0.01,
        ),
        ModelInfo(
            id="gpt-4",
            name="GPT-4",
            provider=ProviderType.OPENAI,
            category=ModelCategory.REASONING,
            context_window=8192,
            max_output_tokens=4096,
            supports_streaming=True,
            supports_functions=True,
            supports_vision=True,
            quality_rating=0.92,
            latency_estimate_ms=2500,
            cost_per_1k_tokens=0.03,
        ),
        # GPT-3.5 models
        ModelInfo(
            id="gpt-3.5-turbo",
            name="GPT-3.5 Turbo",
            provider=ProviderType.OPENAI,
            category=ModelCategory.REASONING,
            context_window=16385,
            max_output_tokens=4096,
            supports_streaming=True,
            supports_functions=True,
            quality_rating=0.75,
            latency_estimate_ms=800,
            cost_per_1k_tokens=0.001,
        ),
        # Coding models
        ModelInfo(
            id="gpt-4-code",
            name="GPT-4 (Coding)",
            provider=ProviderType.OPENAI,
            category=ModelCategory.CODING,
            context_window=128000,
            max_output_tokens=4096,
            supports_streaming=True,
            supports_functions=True,
            quality_rating=0.93,
            latency_estimate_ms=2000,
            cost_per_1k_tokens=0.01,
        ),
        # DALL-E (image generation)
        ModelInfo(
            id="dall-e-3",
            name="DALL-E 3",
            provider=ProviderType.OPENAI,
            category=ModelCategory.IMAGE,
            context_window=0,
            max_output_tokens=0,
            supports_image_generation=True,
            quality_rating=0.85,
            latency_estimate_ms=5000,
            cost_per_image=0.04,
        ),
        ModelInfo(
            id="dall-e-2",
            name="DALL-E 2",
            provider=ProviderType.OPENAI,
            category=ModelCategory.IMAGE,
            context_window=0,
            max_output_tokens=0,
            supports_image_generation=True,
            quality_rating=0.7,
            latency_estimate_ms=3000,
            cost_per_image=0.02,
        ),
    ]

    async def chat_complete(self, request: ChatRequest) -> ChatResponse:
        """Send a chat completion request to OpenAI."""
        if not self.client:
            return ChatResponse(
                content="",
                model=request.model,
                error=self.unavailable_reason or "OpenAI client not initialized",
            )

        try:
            # Build messages
            messages = []
            for msg in request.messages:
                message = {"role": msg.role, "content": msg.content}
                if msg.images:
                    # Handle vision content
                    content_parts = [{"type": "text", "text": msg.content}]
                    for img in msg.images:
                        content_parts.append({"type": "image_url", "image_url": {"url": img}})
                    message["content"] = content_parts
                messages.append(message)

            # Build request params
            params = {
                "model": request.model,
                "messages": messages,
                "temperature": request.temperature,
                "top_p": request.top_p,
            }

            if request.max_tokens:
                params["max_tokens"] = request.max_tokens

            if request.stop:
                params["stop"] = request.stop

            if request.stream:
                params["stream"] = True

            if request.tools:
                params["tools"] = request.tools
                if request.tool_choice:
                    params["tool_choice"] = request.tool_choice

            # Make request
            response = await self.client.chat.completions.create(**params)

            # Parse response
            if request.stream:
                # Handle streaming - collect chunks
                content = ""
                async for chunk in response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        content += chunk.choices[0].delta.content
                usage = {}
            else:
                choice = response.choices[0]
                content = choice.message.content or ""
                finish_reason = choice.finish_reason or "stop"
                usage = response.usage.to_dict() if response.usage else {}

            return ChatResponse(
                content=content,
                model=request.model,
                usage=usage,
                finish_reason=finish_reason,
                raw_response=response.to_dict() if not request.stream else None,
            )

        except openai.APIError as e:
            return ChatResponse(
                content="",
                model=request.model,
                error=f"OpenAI API error: {e.message}",
            )
        except Exception as e:
            return ChatResponse(
                content="",
                model=request.model,
                error=f"Unexpected error: {str(e)}",
            )

    async def generate_image(self, request: ImageRequest) -> ImageResponse:
        """Generate an image using DALL-E."""
        if not self.client:
            return ImageResponse(urls=[], model=request.model, error=self.unavailable_reason)

        try:
            # Map request size to DALL-E sizes
            size_map = {
                "1024x1024": "1024x1024",
                "1792x1024": "1792x1024",
                "1024x1792": "1024x1792",
            }
            dall_e_size = size_map.get(request.size, "1024x1024")

            # DALL-E 3 quality mapping
            quality = request.quality if "dall-e-3" in request.model else None

            response = await self.client.images.generate(
                model=request.model,
                prompt=request.prompt,
                n=request.n,
                size=dall_e_size,
                quality=quality,
            )

            urls = [img.url for img in response.data]
            return ImageResponse(urls=urls, model=request.model)

        except openai.APIError as e:
            return ImageResponse(urls=[], model=request.model, error=f"OpenAI API error: {e.message}")
        except Exception as e:
            return ImageResponse(urls=[], model=request.model, error=f"Unexpected error: {str(e)}")
