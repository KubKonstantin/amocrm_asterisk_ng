from pydantic import BaseModel


__all__ = ["ReflectorPluginConfig"]


class ReflectorPluginConfig(BaseModel):
    internal_number_pattern: str
    internal_number_pattern = r"PJSIP/([^-/]+)"
