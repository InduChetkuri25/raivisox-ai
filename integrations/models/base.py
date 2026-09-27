"""Base model provider interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional
from enum import Enum


class ModelCategory(str, Enum):
    """Model category enumeration."""

    REASONING = "reasoning"
    CODING = "coding"
    VISION = "vision"
    IMAGE = "image"
    VIDEO = "video"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"
    EMBEDDINGS = "embeddings"
    SEARCH = "search"
    LOCAL = "local"


class ProviderType(str, Enum):
    """Provider type enumeration."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    LOCAL = "local"
    MOCK = "mock"


@dataclass
class ModelInfo:
    """Information about an AI model."""

    id: str
    name: str
    provider: ProviderType
    category: ModelCategory
    context_window: int
    max_output_tokens: int
    supports_streaming: bool = True
    supports_functions: bool = False
    supports_vision: bool = False
    supports_image_generation: bool = False
    supports_video_generation: bool = False
    supports_speech: bool = False
    quality_rating: float = 1.0  # 0-1 scale
    latency_estimate_ms: int = 0
    cost_per_1k_tokens: float = 0.0
    cost_per_image: float = 0.0
    available: bool = True


@dataclass
class Message:
    """Chat message."""

    role: str  # "user", "assistant", "system"
    content: str
    images: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatRequest:
    """Chat completion request."""

    model: str
    messages: list[Message]
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    top_p: float = 1.0
    stream: bool = False
    stop: Optional[list[str]] = None
    tools: Optional[list[dict]] = None
    tool_choice: Optional[str] = None


@dataclass
class ChatResponse:
    """Chat completion response."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"
    error: Optional[str] = None
    raw_response: Optional[dict] = None


@dataclass
class CompletionRequest:
    """Text completion request."""

    prompt: str
    model: str
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    top_p: float = 1.0
    stream: bool = False


@dataclass
class CompletionResponse:
    """Text completion response."""

    content: str
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"
    error: Optional[str] = None


@dataclass
class ImageRequest:
    """Image generation request."""

    prompt: str
    model: str
    size: str = "1024x1024"
    quality: str = "standard"
    n: int = 1


@dataclass
class ImageResponse:
    """Image generation response."""

    urls: list[str]
    model: str
    error: Optional[str] = None


@dataclass
class TranscriptionRequest:
    """Speech-to-text request."""

    audio_data: bytes
    model: str
    language: Optional[str] = None


@dataclass
class TranscriptionResponse:
    """Speech-to-text response."""

    text: str
    language: Optional[str] = None
    model: str
    error: Optional[str] = None


@dataclass
class TTSRequest:
    """Text-to-speech request."""

    text: str
    model: str
    voice: Optional[str] = None
    speed: float = 1.0


@dataclass
class TTSResponse:
    """Text-to-speech response."""

    audio_data: bytes
    model: str
    format: str = "mp3"
    error: Optional[str] = None


@dataclass
class EmbeddingRequest:
    """Embedding request."""

    text: str
    model: str


@dataclass
class EmbeddingResponse:
    """Embedding response."""

    embedding: list[float]
    model: str
    usage: dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None


class BaseModelProvider(ABC):
    """Abstract base class for AI model providers."""

    provider_type: ProviderType
    models: list[ModelInfo] = []

    @abstractmethod
    async def chat_complete(self, request: ChatRequest) -> ChatResponse:
        """Send a chat completion request."""
        pass

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Send a text completion request."""
        # Default implementation using chat_complete
        messages = [Message(role="user", content=request.prompt)]
        chat_request = ChatRequest(
            model=request.model,
            messages=messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            top_p=request.top_p,
            stream=request.stream,
        )
        response = await self.chat_complete(chat_request)
        return CompletionResponse(
            content=response.content,
            model=response.model,
            usage=response.usage,
            finish_reason=response.finish_reason,
            error=response.error,
        )

    async def generate_image(self, request: ImageRequest) -> ImageResponse:
        """Generate an image."""
        return ImageResponse(urls=[], model=request.model, error="Image generation not supported")

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResponse:
        """Transcribe audio to text."""
        return TranscriptionResponse(text="", model=request.model, error="Transcription not supported")

    async def text_to_speech(self, request: TTSRequest) -> TTSResponse:
        """Convert text to speech."""
        return TTSResponse(audio_data=b"", model=request.model, error="TTS not supported")

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Generate embeddings."""
        return EmbeddingResponse(embedding=[], model=request.model, error="Embeddings not supported")

    def get_model_info(self, model_id: str) -> Optional[ModelInfo]:
        """Get information about a specific model."""
        for model in self.models:
            if model.id == model_id:
                return model
        return None

    def get_models_by_category(self, category: ModelCategory) -> list[ModelInfo]:
        """Get models by category."""
        return [m for m in self.models if m.category == category and m.available]

    def get_available_models(self) -> list[ModelInfo]:
        """Get all available models."""
        return [m for m in self.models if m.available]
