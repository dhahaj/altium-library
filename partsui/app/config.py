"""Settings, read once from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    db_server: str
    db_name: str
    db_user: str
    db_password: str = field(repr=False)
    custom_prefix: str
    ui_password: str = field(repr=False)
    libs_dirs: tuple[str, ...]
    db_trust_cert: bool = True

    @property
    def template_table(self) -> str:
        return self.custom_prefix + "Template"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)

    ui_password = env.get("PARTSUI_PASSWORD", "")
    if not ui_password.strip():
        raise ConfigError("PARTSUI_PASSWORD is empty. Set it in .env; the Parts Manager won't start without it.")

    db_password = env.get("LIBRARIAN_PASSWORD", "")
    if not db_password:
        raise ConfigError("LIBRARIAN_PASSWORD is empty. The Parts Manager connects as the librarian login.")

    prefix = env.get("CUSTOM_PREFIX", "Custom - ")
    if not prefix.strip():
        raise ConfigError("CUSTOM_PREFIX must not be empty: it is what keeps writes away from Celestial tables.")

    dirs = tuple(d for d in env.get("PARTSUI_LIBS_DIRS", "").split(":") if d.strip())

    return Settings(
        db_server=env.get("DB_SERVER", "mssql,1433"),
        db_name=env.get("DB_NAME", "altium_library"),
        db_user=env.get("LIBRARIAN_USER", "librarian"),
        db_password=db_password,
        custom_prefix=prefix,
        ui_password=ui_password,
        libs_dirs=dirs,
        db_trust_cert=env.get("DB_TRUST_CERT", "yes").lower() in ("1", "yes", "true"),
    )
