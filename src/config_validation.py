"""Configuration validation for claude-swarm.

Enforces environment variable contracts at startup.
Prevents silent failures from missing or weak secrets.
"""

import os
import sys
from dataclasses import dataclass


@dataclass
class RedisConfig:
    """Redis connection configuration with validation."""

    host: str
    port: int
    password: str | None = None
    db: int = 0

    @classmethod
    def from_env(cls) -> "RedisConfig":
        """Load and validate Redis config from environment variables.

        Raises:
            ValueError: If critical configuration is missing or invalid.
        """
        host = os.environ.get("SWARM_REDIS_HOST", "127.0.0.1").strip()
        port_str = os.environ.get("SWARM_REDIS_PORT", "6379").strip()
        password = os.environ.get("SWARM_REDIS_PASSWORD", "").strip()
        db_str = os.environ.get("SWARM_REDIS_DB", "0").strip()

        # Validation: host must not be empty
        if not host:
            raise ValueError(
                "SWARM_REDIS_HOST is required and cannot be empty. Set in .env or environment."
            )

        # Validation: port must be valid integer
        try:
            port = int(port_str)
            if port < 1 or port > 65535:
                raise ValueError
        except ValueError:
            raise ValueError(f"SWARM_REDIS_PORT must be a valid port (1-65535), got: {port_str}")

        # Validation: db must be valid integer
        try:
            db = int(db_str)
            if db < 0 or db > 15:
                raise ValueError
        except ValueError:
            raise ValueError(f"SWARM_REDIS_DB must be 0-15, got: {db_str}")

        # Fail-closed in non-dev environments; warn with HYDRA_ENV=dev.
        hydra_env = os.environ.get("HYDRA_ENV", "prod").lower()
        if not password:
            if hydra_env == "dev":
                print(
                    "WARNING: SWARM_REDIS_PASSWORD not set. "
                    "Continuing with no password (HYDRA_ENV=dev).",
                    file=sys.stderr,
                )
            else:
                raise ValueError(
                    "SWARM_REDIS_PASSWORD is required in non-dev environments. "
                    "Set HYDRA_ENV=dev to allow unauthenticated Redis for local development."
                )
        elif len(password) < 32:
            print(
                f"WARNING: SWARM_REDIS_PASSWORD is {len(password)} chars; "
                "recommend >=32 chars for production.",
                file=sys.stderr,
            )

        return cls(host=host, port=port, password=password or None, db=db)

    def to_dict(self) -> dict:
        """Return config as dictionary for redis.ConnectionPool."""
        return {
            "host": self.host,
            "port": self.port,
            "password": self.password,
            "db": self.db,
            "decode_responses": True,
            "socket_timeout": 5,
            "socket_connect_timeout": 5,
            "retry_on_timeout": True,
        }


@dataclass
class FleetConfig:
    """Fleet host configuration."""

    miniboss_host: str
    giga_host: str

    @classmethod
    def from_env(cls) -> "FleetConfig":
        """Load and validate fleet config from environment variables.

        Raises:
            ValueError: If critical configuration is missing.
        """
        miniboss = os.environ.get("MINIBOSS_HOST", "127.0.0.1").strip()
        giga = os.environ.get("GIGA_HOST", "127.0.0.1").strip()

        if not miniboss:
            raise ValueError("MINIBOSS_HOST is required and cannot be empty")
        if not giga:
            raise ValueError("GIGA_HOST is required and cannot be empty")

        return cls(miniboss_host=miniboss, giga_host=giga)


def validate_all_config() -> dict:
    """Validate all configuration at startup.

    Returns:
        dict with 'redis' and 'fleet' keys

    Raises:
        ValueError: If any critical config is invalid.
    """
    try:
        redis_config = RedisConfig.from_env()
        fleet_config = FleetConfig.from_env()
        return {
            "redis": redis_config,
            "fleet": fleet_config,
        }
    except ValueError as e:
        print(f"FATAL: Configuration validation failed: {e}", file=sys.stderr)
        sys.exit(1)
