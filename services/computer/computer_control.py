"""Computer control — unified interface over browser, terminal, and filesystem."""

from enum import Enum

from services.computer.browser_controller import BrowserController


class ControlCapability(str, Enum):
    """Capabilities the computer-control layer can expose."""

    BROWSER_NAVIGATE = "browser_navigate"
    BROWSER_CLICK = "browser_click"
    BROWSER_TYPE = "browser_type"
    BROWSER_SCREENSHOT = "browser_screenshot"
    TERMINAL_EXECUTE = "terminal_execute"
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    FILESYSTEM_LIST = "filesystem_list"


class ComputerControl:
    """Abstraction for computer control capabilities.

    Wraps tools like browser control, terminal access, file system,
    and other system-level operations under a unified interface.
    """

    def __init__(self):
        self.browser = BrowserController()
        self._capabilities: dict[str, bool] = {
            ControlCapability.BROWSER_NAVIGATE.value: True,
            ControlCapability.BROWSER_CLICK.value: True,
            ControlCapability.BROWSER_TYPE.value: True,
            ControlCapability.BROWSER_SCREENSHOT.value: True,
            ControlCapability.TERMINAL_EXECUTE.value: True,
            ControlCapability.FILESYSTEM_READ.value: True,
            ControlCapability.FILESYSTEM_WRITE.value: True,
            ControlCapability.FILESYSTEM_LIST.value: True,
        }

    def has_capability(self, capability: str | ControlCapability) -> bool:
        key = capability.value if isinstance(capability, ControlCapability) else capability
        return self._capabilities.get(key, False)

    def enable(self, capability: str | ControlCapability) -> None:
        key = capability.value if isinstance(capability, ControlCapability) else capability
        self._capabilities[key] = True

    def disable(self, capability: str | ControlCapability) -> None:
        key = capability.value if isinstance(capability, ControlCapability) else capability
        self._capabilities[key] = False

    def available_capabilities(self) -> dict[str, bool]:
        return dict(self._capabilities)
