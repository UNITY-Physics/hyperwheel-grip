"""The Pi's accounts, read from the system's account files (so tests can use a folder)."""

from __future__ import annotations

from dataclasses import dataclass

from .installer import Layout

ADMIN_GROUP = "sudo"


@dataclass(frozen=True)
class Accounts:
    """Read from the system's account files (under the layout's root)."""
    users: dict[str, int]  # name -> primary group ID
    groups: dict[str, tuple[int, set[str]]]  # name -> (ID, members)
    hashes: dict[str, str]

    @classmethod
    def read(cls, layout: Layout) -> Accounts:
        def rows(name: str) -> list[list[str]]:
            try:
                return [line.split(":") for line in layout.p(f"/etc/{name}").read_text().splitlines()
                        if line and not line.startswith("#")]
            except FileNotFoundError:
                return []
        users = {r[0]: int(r[3]) for r in rows("passwd") if len(r) >= 4 and r[3].isdigit()}
        groups = {r[0]: (int(r[2]), {m for m in r[3].split(",") if m}) for r in rows("group")
                  if len(r) >= 4 and r[2].isdigit()}
        hashes = {r[0]: r[1] for r in rows("shadow") if len(r) >= 2}
        return cls(users, groups, hashes)

    def exists(self, user: str) -> bool:
        return user in self.users

    def in_group(self, user: str, group: str) -> bool:
        if group not in self.groups or user not in self.users:
            return False
        gid, members = self.groups[group]
        return user in members or self.users[user] == gid

    def has_password(self, user: str) -> bool:
        """A password someone can log in with: not empty, not locked ("!", "*")."""
        h = self.hashes.get(user, "")
        return bool(h) and not h.startswith(("!", "*")) and h != "x"

    def admins_with_password(self, besides: str) -> list[str]:
        return sorted(u for u in self.users if u != besides and self.in_group(u, ADMIN_GROUP)
                      and self.has_password(u))
