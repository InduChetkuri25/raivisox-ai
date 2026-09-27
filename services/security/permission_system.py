"""Role and policy management for the security kernel."""

from enum import Enum

from services.security.security_kernel import Permission, SecurityKernel


class Role(str, Enum):
    USER = "user"
    OPERATOR = "operator"
    ADMIN = "admin"
    SERVICE = "service"


class Policy:
    """Named security policy that can be applied to actors."""

    def __init__(self, name: str, permissions: list[Permission], role: Role = Role.USER):
        self.name = name
        self.permissions = permissions
        self.role = role

    def apply_to(self, kernel: SecurityKernel, actor_id: str) -> None:
        for perm in self.permissions:
            kernel.grant_permission(actor_id, perm)


class PermissionSystem:
    """Manages roles, policies, and permission assignment."""

    def __init__(self):
        self._policies: dict[str, Policy] = {}
        self._roles: dict[Role, list[Permission]] = {}
        self._actor_roles: dict[str, Role] = {}

        # Default role permissions
        self._roles[Role.USER] = [
            Permission.READ_FILES,
            Permission.NETWORK_REQUEST,
        ]
        self._roles[Role.OPERATOR] = [
            Permission.READ_FILES,
            Permission.WRITE_FILES,
            Permission.EXECUTE_TERMINAL,
            Permission.BROWSER_NAVIGATE,
            Permission.BROWSER_CLICK,
            Permission.NETWORK_REQUEST,
        ]
        self._roles[Role.ADMIN] = [
            Permission.EXECUTE_CODE,
            Permission.READ_FILES,
            Permission.WRITE_FILES,
            Permission.DELETE_FILES,
            Permission.EXECUTE_TERMINAL,
            Permission.BROWSER_NAVIGATE,
            Permission.BROWSER_CLICK,
            Permission.NETWORK_REQUEST,
            Permission.ACCESS_MEMORY,
            Permission.MODIFY_CONFIG,
            Permission.ADMIN,
        ]

    def create_policy(self, name: str, permissions: list[Permission]) -> Policy:
        policy = Policy(name, permissions)
        self._policies[name] = policy
        return policy

    def assign_role(self, kernel: SecurityKernel, actor_id: str, role: Role) -> None:
        self._actor_roles[actor_id] = role
        for perm in self._roles.get(role, []):
            kernel.grant_permission(actor_id, perm)

    def get_actor_role(self, actor_id: str) -> Role | None:
        return self._actor_roles.get(actor_id)

    def get_policies(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "permissions": [p.value for p in p.permissions],
                "role": p.role.value,
            }
            for p in self._policies.values()
        ]
