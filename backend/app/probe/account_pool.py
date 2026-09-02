from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .errors import ProbeError


class ProbeAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ref: str = Field(min_length=1, max_length=160)
    online: bool
    is_wplus: bool
    token_present: bool
    phone_present: bool
    risk_status: str = Field(max_length=40)
    remaining: int = Field(ge=0, le=100_000)
    cooldown_until: str | None = None
    failure_count: int = Field(default=0, ge=0)
    # Internal-only fixture fields. public_view intentionally excludes them.
    token: str | None = Field(default=None, repr=False)
    phone: str | None = Field(default=None, repr=False)

    def eligible(self, *, now: datetime) -> bool:
        cooldown = _parse(self.cooldown_until)
        return (
            self.online and self.is_wplus and self.token_present and self.phone_present
            and self.risk_status.lower() == "normal" and self.remaining > 0
            and (cooldown is None or cooldown <= now)
        )

    def public_view(self) -> dict[str, object]:
        return {
            "account_ref": self.account_ref,
            "online": self.online,
            "is_wplus": self.is_wplus,
            "risk_status": self.risk_status,
            "remaining": self.remaining,
            "cooldown_until": self.cooldown_until,
            "failure_count": self.failure_count,
        }


class FixtureProbeAccountPool:
    """Offline account source; intentionally has no HTTP or credential access."""

    def __init__(self, accounts: Iterable[ProbeAccount]) -> None:
        self._accounts = tuple(accounts)

    def select(self, *, now: datetime | None = None) -> ProbeAccount:
        current = now or datetime.now(timezone.utc)
        for account in self._accounts:
            if account.eligible(now=current):
                return account
        raise ProbeError("wplus_account_unavailable", "没有符合 Probe 条件的离线账号。")

    def accounts(self) -> tuple[ProbeAccount, ...]:
        return self._accounts


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)
