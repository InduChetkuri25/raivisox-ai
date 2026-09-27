"""Anthropic API adapter."""

from typing import Optional

from integrations.models.base import (
    BaseModelProvider,
    ProviderType,
    ModelCategory,
    ModelInfo,
    ChatRequest,
    ChatResponse,
)

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False


class AnthropicAdapter(BaseModelProvider):
    """Anthropic API adapter for Claude models."""

    provider_type = ProviderType.ANTHROPIC

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        """Initialize the Anthropic adapter."""
        super().__init__()
        self.api_key = api_key
        self.base_url = base_url or "https://api.anthropic.com"

        if not ANTHROPIC_AVAILABLE:
            raise ImportError(
                "anthropic package not installed. Install with: pip install anthropic"
            )

        if api_key:
            self.client = anthropic.AsyncAnthropic(
                api_key=api_key,
                base_url=self.base_url,
            )
        else:
            self.client = None
            self._mark_unavailable("No API key provided")

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
        # Claude 3.5
        ModelInfo(
            id="claude-3-5-sonnet",
            name="Claude 3.5 Sonnet",
            provider=ProviderType.ANTHROPIC,
            category=ModelCategory.REASONING,
            context_window=200000,
            max_output_tokens=8192,
            supports_streaming=True,
            supports_functions=True,
            supports_vision=True,
            quality_rating=0.94,
            latency_estimate_ms=1500,
            cost_per_1k_tokens=0.003,
        ),
        # Claude 3 Opus
        ModelInfo(
            id="claude-3-opus",
            name="Claude 3 Opus",
            provider=ProviderType.ANTHROPIC,
            category=ModelCategory.REASONING,
            context_window=200000,
            max_output_tokens=4096,
            supports_streaming=True,
            supports_functions=True,
            supports_vision=True,
            quality_rating=0.95,
            latency_estimate_ms=2000,
            cost_per_1k_tokens=0.015,
        ),
        # Claude 3 Sonnet
        ModelInfo(
            id="claude-3-sonnet",
            name="Claude 3 Sonnet",
            provider=ProviderType.ANTHROPIC,
            category=ModelCategory.REASONING,
            context_window=200000,
            max_output_tokens=4096,
            supports_streaming=True,
            supports_functions=True,
            supports_vision=True,
            quality_rating=0.88,
            latency_estimate_ms=1200,
            cost_per_1k_tokens=0.003,
        ),
        # Claude 3 Haiku
        ModelInfo(
            id="claude-3-haiku",
            name="Claude 3 Haiku",
            provider=ProviderType.ANTHROPIC,
            category=ModelCategory.REASONING,
            context_window=200000,
            max_output_tokens=4096,
            supports_streaming=True,
            supports_functions=True,
            supports_vision=True,
            quality_rating=0.75,
            latency_estimate_ms=500,
            cost_per_1k_tokens=0.00025,
        ),
        # Coding optimized
        ModelInfo(
            id="claude-3-5-sonnet-coding",
            name="Claude 3.5 Sonnet (Coding)",
            provider=ProviderType.ANTHROPIC,
            category=ModelCategory.CODING,
            context_window=200000,
            max_output_tokens=8192,
            supports_streaming=True,
            supports_functions=True,
            quality_rating=0.93,
            latency_estimate_ms=1500,
            cost_per_1k_tokens=0.003,
        ),
    ]

    async def chat_complete(self, request: ChatRequest) -> ChatResponse:
        """Send a chat completion request to Anthropic."""
        if not self.client:
            return ChatResponse(
                content="",
                model=request.model,
                error=self.unavailable_reason or "Anthropic client not initialized",
            )

        try:
            # Convert messages to Anthropic format
            system_message = None
            messages = []

            for msg in request.messages:
                if msg.role == "system":
                    system_message = msg.content
                elif msg.role == "user":
                    if msg.images:
                        # Handle vision content
                        content = []
                        content.append({"type": "text", "text": msg.content})
                        for img in msg.images:
                            content.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": img,  # Assume base64 encoded
                                }
                            })
                        messages.append({"role": "user", "content": content})
                    else:
                        messages.append({"role": "user", "content": msg.content})
                elif msg.role == "assistant":
                    messages.append({"role": "assistant", "content": msg.content})

            # Build request params
            params = {
                "model": request.model,
                "messages": messages,
                "temperature": request.temperature,
                "top_p": request.top_p,
                "stream": request.stream,
            }

            if system_message:
                params["system"] = system_message

            if request.max_tokens:
                params["max_tokens"] = request.max_tokens
            else:
                params["max_tokens"] = 4096  # Default for Anthropic

            if request.stop:
                params["stop_sequences"] = request.stop

            # Make request
            response = await self.client.messages.create(**params)

            # Parse response
            if request.stream:
                content = ""
                async for chunk in response:
                    if chunk.content and chunk.content[0].text:
                        content += chunk.content[0].text
                usage = {}
            else:
                content = "".join(block.text for block in response.content)
                usage = {
                    "prompt_tokens": response.usage.output_tokens + response.usage.input_tokens if hasattr(response, 'usage') else 0,
                    "completion_tokens": response.usage.output_tokens if hasattr(response, 'usage') else 0,
                    "total_tokens": response.usage.input_tokens + response.usage.output_tokens if hasattr(response, 'usage') else 0,
                }

            finish_reason = "stop"

            return ChatResponse(
                content=content,
                model=request.model,
                usage=usage,
                finish_reason=finish_reason,
            )

        except anthropic.APIError as e:
            return ChatResponse(
                content="",
                model=request.model,
                error=f"Anthropic API error: {e.message}",
            )
        except Exception as e:
            return ChatResponse(
                content="",
                model=request.model,
                error=f"Unexpected error: {str(e)}",
            )
