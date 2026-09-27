"""Security kernel — controls all privileged execution."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time
import uuid

from services.security.audit_trail import AuditTrail


class Permission(str, Enum):
    EXECUTE_CODE = "execute_code"
    READ_FILES = "read_files"
    WRITE_FILES = "write_files"
    DELETE_FILES = "delete_files"
    EXECUTE_TERMINAL = "execute_terminal"
    BROWSER_NAVIGATE = "browser_navigate"
    BROWSER_CLICK = "browser_click"
    NETWORK_REQUEST = "network_request"
    ACCESS_MEMORY = "access_memory"
    MODIFY_CONFIG = "modify_config"
    ADMIN = "admin"


@dataclass
class ActionContext:
    """Context for a security-audited action."""

    actor_id: str
    actor_type: str = "user"
    action: str = ""
    resource: str = ""
    permissions_requested: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "actor_id": self.actor_id,
            "actor_type": self.actor_type,
            "action": self.action,
            "resource": self.resource,
            "permissions_requested": self.permissions_requested,
            "timestamp": self.timestamp,
        }


@dataclass
class SecurityDecision:
    """Result of a security check."""

    allowed: bool
    granted_permissions: list[str] = field(default_factory=list)
    denied_permissions: list[str] = field(default_factory=list)
    reason: str = ""
    decision_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "allowed": self.allowed,
            "granted": self.granted_permissions,
            "denied": self.denied_permissions,
            "reason": self.reason,
        }


class SecurityKernel:
    """Central security kernel that authorizes all privileged actions."""

    def __init__(self):
        self._policy: dict[str, list[str]] = {}  # actor_id -> permissions
        self._deny_list: set[str] = set()
        self._allowed_actions: set[str] = set()

    def grant_permission(self, actor_id: str, permission: Permission) -> None:
        self._policy.setdefault(actor_id, [])
        if permission.value not in self._policy[actor_id]:
            self._policy[actor_id].append(permission.value)

    def revoke_permission(self, actor_id: str, permission: Permission) -> None:
        if actor_id in self._policy and permission.value in self._policy[actor_id]:
            self._policy[actor_id].remove(permission.value)

    def has_permission(self, actor_id: str, permission: Permission) -> bool:
        return permission.value in self._policy.get(actor_id, [])

    def authorize(
        self,
        context: ActionContext,
    ) -> SecurityDecision:
        """Authorize an action based on actor permissions and policy."""
        actor_perms = set(self._policy.get(context.actor_id, []))
        requested = set(context.permissions_requested)

        # Admin bypass for admin permission
        if Permission.ADMIN.value in actor_perms and Permission.ADMIN.value in requested:
            self._allowed_actions.add(context.request_id)
            return SecurityDecision(
                allowed=True,
                granted_permissions=list(requested),
                reason="Admin override",
            )

        # Check each requested permission
        granted = []
        denied = []
        for perm in requested:
            if perm in self._deny_list:
                denied.append(perm)
            elif perm in actor_perms:
                granted.append(perm)
            else:
                denied.append(perm)

        allowed = len(denied) == 0 and len(granted) > 0
        reason = "All permissions granted" if allowed else f"Missing: {', '.join(denied)}"

        if allowed:
            self._allowed_actions.add(context.request_id)

        return SecurityDecision(
            allowed=allowed,
            granted_permissions=granted,
            denied_permissions=denied,
            reason=reason,
        )

    def record_audit(self, context: ActionContext, decision: SecurityDecision) -> None:
        """Record an audit event for the action."""
        AuditTrail.instance().log(
            actor_id=context.actor_id,
            action=context.action,
            resource=context.resource,
            allowed=decision.allowed,
            request_id=context.request_id,
            details={"granted": decision.granted_permissions, "denied": decision.denied_permissions, "reason": decision.reason},
        )

    def authorize_and_log(
        self,
        actor_id: str,
        action: str,
        resource: str,
        permissions: list[str],
        actor_type: str = "user",
    ) -> SecurityDecision:
        """Authorize an action and log the audit trail."""
        context = ActionContext(
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            resource=resource,
            permissions_requested=permissions,
        )
        decision = self.authorize(context)
        self.record_audit(context, decision)
        return decision

    def allow_action(self, action: str) -> None:
        self._allowed_actions.add(action)

    def is_action_allowed(self, action: str) -> bool:
        return action in self._allowed_actions
