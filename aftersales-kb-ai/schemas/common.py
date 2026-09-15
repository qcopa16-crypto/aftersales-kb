"""Shared schema tokens/helpers."""
import re

_UNIT_ID_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
