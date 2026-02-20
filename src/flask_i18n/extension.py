import os
import json
from pathlib import Path
from typing import Any, Dict, Optional

import click
import requests

from flask import Flask, g, request, current_app


class Translations:
    """
    Flask extension for JSON-based translations.

    Features:
      - Loads translations per request into flask.g (translations_<domain>, fallback_translations_<domain>)
      - Adds Jinja filter: {{ 'key'|trans(domain='messages', name='Igor') }}
      - Optional preload at startup
      - Optional cache integration via app.extensions['translations_cache'] (must have get/set)
      - CLI pull command from the backend:
          flask translations pull <branch>
    """

    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        app.config.setdefault('TRANSLATIONS_DIR', 'translations')
        app.config.setdefault('SUPPORTED_DOMAINS', ('messages',))
        app.config.setdefault('SUPPORTED_LOCALES', ('en',))
        app.config.setdefault('FALLBACK_LOCALE', 'en')
        app.config.setdefault('TRANSLATIONS_HEADER', 'SELECTED-LOCALE')
        app.config.setdefault('TRANSLATIONS_PRELOAD', True)
        app.config.setdefault('TRANSLATIONS_CACHE_TIMEOUT', None)
        app.config.setdefault('TRANSLATIONS_PROVIDER_AUTH_HEADER', None)
        app.config.setdefault('TRANSLATIONS_PROVIDER_URL_TEMPLATE', None)
        app.config.setdefault('TRANSLATIONS_PROVIDER_TOKEN', None)
        app.config.setdefault('TRANSLATIONS_PROVIDER_TIMEOUT', 20)

        app.add_template_filter(self._jinja_trans_filter, name='trans')
        app.before_request(self._before_request)

        self._register_cli(app)

        app.extensions['translations'] = self

        if app.config['TRANSLATIONS_PRELOAD']:
            with app.app_context():
                self._preload_all()

    def get_request_locale(self) -> str:
        return g.get('request_locale', current_app.config['FALLBACK_LOCALE'])

    def t(self, key: str, domain: str = 'messages', parameters: Optional[Dict[str, str]] = None) -> str:
        translations = g.get(f'translations_{domain}')

        translation = translations.get(key) if translations else None

        if translation is None:
            fallback = g.get(f'fallback_translations_{domain}')

            if not fallback:
                return key

            translation = fallback.get(key, key)

        if parameters:
            for k, v in parameters.items():
                translation = translation.replace(f'{k}', str(v))

        return translation

    def _jinja_trans_filter(self, key, domain='messages', **kwargs) -> str:
        return self.t(key, domain, parameters=kwargs or None)

    def _before_request(self) -> None:
        header = current_app.config['TRANSLATIONS_HEADER']
        request_locale = request.headers.get(header, current_app.config['FALLBACK_LOCALE'])

        g.request_locale = request_locale

        self._load_translations(request_locale)

    def _load_translations(self, request_locale: str) -> None:
        supported_locales = current_app.config['SUPPORTED_LOCALES']
        fallback_locale = current_app.config['FALLBACK_LOCALE']
        domains = current_app.config['SUPPORTED_DOMAINS']

        if request_locale not in supported_locales:
            request_locale = fallback_locale

        for domain in domains:
            translations = self._cache_get(domain, request_locale)

            setattr(g, f'translations_{domain}', translations)

            if request_locale == fallback_locale:
                setattr(g, f'fallback_translations_{domain}', translations)
            else:
                setattr(g, f'fallback_translations_{domain}', self._cache_get(domain, fallback_locale))

    def _preload_all(self) -> None:
        domains = current_app.config['SUPPORTED_DOMAINS']
        locales = current_app.config['SUPPORTED_LOCALES']

        for domain in domains:
            for locale in locales:
                try:
                    self._cache_set(domain, locale, self._read_translations_file(domain, locale))
                except FileNotFoundError:
                    current_app.logger.warning(
                        'Translations file %s_%s.json not found. Consider running: flask translations pull <branch>',
                        domain, locale,
                    )

    def _cache_key(self, domain: str, locale: str) -> str:
        return f'{domain}_{locale}'

    def _cache_get(self, domain: str, locale: str) -> Dict[str, Any]:
        """
        If app.extensions['translations_cache'] exists, use it. Otherwise use in-memory cache.
        Cache object is expected to have get(key) and set(key, value, **kwargs).
        """
        cache = current_app.extensions.get('translations_cache')
        key = self._cache_key(domain, locale)

        if cache is None:
            store = current_app.extensions.setdefault('_translations_memcache', {})

            if key not in store:
                store[key] = self._read_translations_file(domain, locale)

            return store[key]

        val = cache.get(key)

        if val is None:
            val = self._read_translations_file(domain, locale)

            self._cache_set(domain, locale, val)

        return val

    def _cache_set(self, domain: str, locale: str, value: Dict[str, Any]) -> None:
        cache = current_app.extensions.get('translations_cache')

        key = self._cache_key(domain, locale)

        if cache is None:
            store = current_app.extensions.setdefault('_translations_memcache', {})

            store[key] = value

            return

        timeout = current_app.config['TRANSLATIONS_CACHE_TIMEOUT']

        try:
            if timeout is None:
                cache.set(key, value)
            else:
                cache.set(key, value, timeout=timeout)
        except TypeError:
            cache.set(key, value)

    def _read_translations_file(self, domain: str, locale: str) -> Dict[str, Any]:
        base = current_app.config['TRANSLATIONS_DIR']

        path = os.path.join(base, f'{domain}_{locale}.json')

        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _register_cli(self, app) -> None:
        @app.cli.group('translations')
        def translations_group():
            """Manage translation files."""
            pass

        @translations_group.command('pull', short_help='Pull translations from translations provider')
        @click.argument('branch', required=True)
        @click.option('--force', is_flag=True, help='Overwrite existing files.')
        def pull(branch: str, force: bool) -> None:
            self._cli_pull(branch=branch, force=force)

    def _cli_pull(self, branch: str, force: bool) -> None:
        cfg = current_app.config

        token = cfg.get('TRANSLATIONS_PROVIDER_TOKEN') or os.getenv('TRANSLATIONS_PROVIDER_TOKEN')

        if not token:
            raise click.ClickException(
                "Missing TRANSLATIONS_PROVIDER_TOKEN. Set app.config['TRANSLATIONS_PROVIDER_TOKEN'] or env var TRANSLATIONS_PROVIDER_TOKEN."
            )

        base_url = cfg.get('TRANSLATIONS_PROVIDER_URL_TEMPLATE')
        translations_dir = cfg.get('TRANSLATIONS_DIR', 'translations')
        auth_header = cfg.get('TRANSLATIONS_PROVIDER_AUTH_HEADER')
        timeout = cfg.get('TRANSLATIONS_PROVIDER_TIMEOUT', 20)

        try:
            resp = requests.get(base_url.format(branch=branch), headers={auth_header: token}, timeout=timeout)

            resp.raise_for_status()

            payload = resp.json()
        except requests.RequestException as e:
            raise click.ClickException(f'Request failed: {e}')
        except ValueError as e:
            raise click.ClickException(f'Invalid JSON from server: {e}')

        if not isinstance(payload, dict):
            raise click.ClickException(f'Unexpected response type: {type(payload).__name__} (expected dict)')

        written = 0
        skipped = 0

        Path(translations_dir).mkdir(parents=True, exist_ok=True)

        for locale, domains in payload.items():
            if not isinstance(domains, dict):
                continue

            for domain, translations in domains.items():
                if not isinstance(translations, dict):
                    continue

                out_path = Path(translations_dir) / f'{domain}_{locale}.json'

                if out_path.exists() and not force:
                    skipped += 1

                    click.echo(f'skip  {domain}/{locale} (exists, use --force): {out_path}')

                    continue

                tmp_path = out_path.with_suffix(out_path.suffix + '.tmp')

                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(translations, f, ensure_ascii=False, indent=2, sort_keys=True)

                    f.write('\n')

                tmp_path.replace(out_path)

                self._cache_set(domain, locale, translations)

                written += 1

                click.echo(f'pull  {domain}/{locale} -> {out_path}')

        click.echo(f'Done. Written: {written}, skipped: {skipped}')
