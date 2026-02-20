from .extension import Translations

from flask import current_app


__all__ = ['Translations', 't']
__version__ = '0.1.0'

def t(key: str, domain: str = 'messages', parameters: dict | None = None) -> str:
    ext: Translations = current_app.extensions['translations']

    return ext.t(key, domain, parameters)
