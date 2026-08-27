# -*- coding: utf-8 -*-
"""image.apisaver 账号池自动导入配置。"""
from config.env_loader import apply_env_overrides

IMAGE_API_AUTO_IMPORT: bool = False
IMAGE_API_BASE: str = "http://127.0.0.1:3100"
IMAGE_API_AUTH_KEY: str = ""
IMAGE_API_TIMEOUT: int = 20

apply_env_overrides(globals(), {
    "IMAGE_API_AUTO_IMPORT": "bool",
    "IMAGE_API_BASE": "str",
    "IMAGE_API_AUTH_KEY": "str",
    "IMAGE_API_TIMEOUT": "int",
})
