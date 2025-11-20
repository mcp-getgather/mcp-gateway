from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.settings import settings


class Container(BaseModel):
    id: str
    name: str
    hostname: str
    ip: str | None
    status: Literal["running", "exited"]
    started_at: datetime  # datetime in UTC
    checkpointed: bool

    info: dict[str, Any] = Field(exclude=True)
    network_name: str = Field(exclude=True)

    @property
    def validated_ip(self) -> str:
        if self.ip is None:
            raise RuntimeError(f"Container {self.name} has no IP address")
        return self.ip

    @classmethod
    def from_inspect(cls, info: dict[str, Any], *, network_name: str) -> "Container":
        networks = info["NetworkSettings"].get("Networks", {})
        return cls(
            id=info["Id"][:12],
            name=info["Name"].lstrip("/"),
            hostname=info["Config"]["Hostname"],
            ip=networks[network_name]["IPAddress"] if network_name in networks else None,
            status=info["State"]["Status"],
            started_at=datetime.fromisoformat(info["State"]["StartedAt"]).astimezone(timezone.utc),
            checkpointed=info["State"].get("Checkpointed", False),
            info=info,
            network_name=network_name,
        )

    @property
    def mount_dir(self) -> Path:
        return self.mount_dir_for_hostname(self.hostname)

    @classmethod
    def mount_dir_for_hostname(cls, hostname: str) -> Path:
        """Mount directory name is the same as hostname."""
        path = settings.container_mount_parent_dir / hostname
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def metadata_file(self) -> Path:
        return self.metadata_file_for_hostname(self.hostname)

    @classmethod
    def metadata_file_for_hostname(cls, hostname: str) -> Path:
        return cls.mount_dir_for_hostname(hostname) / "metadata.json"
