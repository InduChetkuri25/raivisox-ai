"""Browser controller abstraction."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time


class BrowserEventType(str, Enum):
    NAVIGATED = "navigated"
    CLICKED = "clicked"
    TYPED = "typed"
    SCROLLED = "scrolled"
    SCREENSHOT = "screenshot"
    TEXT_EXTRACTED = "text_extracted"
    FORM_SUBMITTED = "form_submitted"
    ERROR = "error"


@dataclass
class BrowserEvent:
    type: BrowserEventType
    description: str
    data: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "description": self.description,
            "data": self.data,
            "timestamp": self.timestamp,
        }


@dataclass
class BrowserSession:
    """A browser session context."""

    id: str
    url: str = ""
    title: str = ""
    is_active: bool = True
    history: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "is_active": self.is_active,
            "history": self.history,
            "created_at": self.created_at,
        }


class BrowserController:
    """Abstraction for browser automation and control.

    In production, this wraps Playwright, Selenium, or similar.
    Development mode uses a mock implementation.
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._sessions: dict[str, BrowserSession] = {}
        self._event_log: list[BrowserEvent] = []

    def create_session(self, session_id: str) -> BrowserSession:
        session = BrowserSession(id=session_id)
        self._sessions[session_id] = session
        return session

    def navigate(self, session_id: str, url: str) -> dict:
        session = self._sessions.get(session_id)
        if not session:
            return {"error": f"Session '{session_id}' not found"}
        session.url = url
        session.title = self._extract_title(url)
        session.history.append(url)
        self._log(BrowserEvent(BrowserEventType.NAVIGATED, f"Navigated to {url}", {"url": url}))
        return {"url": url, "title": session.title, "session": session_id}

    def click(self, session_id: str, selector: str) -> dict:
        self._log(BrowserEvent(BrowserEventType.CLICKED, f"Clicked '{selector}'", {"selector": selector}))
        return {"action": "click", "selector": selector, "session": session_id}

    def type(self, session_id: str, selector: str, text: str) -> dict:
        self._log(BrowserEvent(BrowserEventType.TYPED, f"Typed into '{selector}'", {"selector": selector, "text_length": len(text)}))
        return {"action": "type", "selector": selector, "text_length": len(text), "session": session_id}

    def screenshot(self, session_id: str) -> dict:
        """Returns a placeholder screenshot reference (mock in development)."""
        self._log(BrowserEvent(BrowserEventType.SCREENSHOT, "Screenshot captured", {"session": session_id}))
        return {
            "session": session_id,
            "screenshot_ref": f"screenshot_{session_id}_{int(time.time() * 1000)}",
            "note": "Mock screenshot — real implementation would return image data",
        }

    def extract_text(self, session_id: str, selector: str) -> dict:
        self._log(BrowserEvent(BrowserEventType.TEXT_EXTRACTED, f"Extracted text from '{selector}'", {"selector": selector}))
        return {
            "session": session_id,
            "selector": selector,
            "text": "(mock extracted text)",
            "note": "Mock extraction — real implementation would return page content",
        }

    def fill_form(self, session_id: str, fields: dict[str, str]) -> dict:
        self._log(BrowserEvent(BrowserEventType.FORM_SUBMITTED, "Form filled", {"fields_count": len(fields)}))
        return {"action": "fill_form", "fields_count": len(fields), "session": session_id}

    def close_session(self, session_id: str) -> dict:
        session = self._sessions.pop(session_id, None)
        if session:
            session.is_active = False
            self._log(BrowserEvent(BrowserEventType.ERROR, f"Session '{session_id}' closed", {}))
        return {"closed": session is not None, "session": session_id}

    def get_session(self, session_id: str) -> BrowserSession | None:
        return self._sessions.get(session_id)

    def active_sessions(self) -> list[BrowserSession]:
        return [s for s in self._sessions.values() if s.is_active]

    def _extract_title(self, url: str) -> str:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc or "unknown"
        return f"{domain} — RAIVISOX Mock Browser"

    def _log(self, event: BrowserEvent) -> None:
        self._event_log.append(event)

    def event_log(self, session_id: str | None = None) -> list[BrowserEvent]:
        if session_id:
            return [e for e in self._event_log if session_id in str(e.data.get("session", ""))]
        return list(self._event_log)
