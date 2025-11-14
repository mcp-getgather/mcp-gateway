from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.settings import settings


class Container(BaseModel):
    id: str
    hostname: str
    ip: str
    status: Literal["running"]
    started_at: datetime  # datetime in UTC

    info: dict[str, Any] = Field(exclude=True)
    network_name: str = Field(exclude=True)

    @classmethod
    def from_inspect(cls, info: dict[str, Any], *, network_name: str) -> "Container":
        return cls(
            id=info["Id"][:12],
            hostname=info["Config"]["Hostname"],
            ip=info["NetworkSettings"]["Networks"][network_name]["IPAddress"],
            status=info["State"]["Status"],
            started_at=datetime.fromisoformat(info["State"]["StartedAt"]).astimezone(timezone.utc),
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
