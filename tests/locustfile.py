"""Locust load testing configuration for KERN.

Run with:
    locust -f tests/locustfile.py --host http://127.0.0.1:8000

Or headless:
    locust -f tests/locustfile.py --host http://127.0.0.1:8000 \
           --headless -u 50 -r 10 --run-time 60s
"""
from __future__ import annotations

try:
    from locust import HttpUser, between, task
except ImportError:
    # Stub classes so the file is importable for scaffold validation
    class between:  # type: ignore[no-redef]
        def __init__(self, *_args, **_kwargs): pass
    def task(fn=None, *_args, **_kwargs):  # type: ignore[no-redef]
        if fn is None:
            return lambda f: f
        return fn
    class HttpUser:  # type: ignore[no-redef]
        wait_time = None
        def __init_subclass__(_cls, **_kwargs): pass


class KERNUser(HttpUser):
    """Simulates a typical KERN dashboard user."""

    wait_time = between(1, 3)

    @task(3)
    def health_check(self):
        self.client.get("/health/live")

    @task(5)
    def load_dashboard(self):
        self.client.get("/dashboard")

    @task(2)
    def load_static_css(self):
        self.client.get("/static/dashboard.css")

    @task(2)
    def load_static_js(self):
        self.client.get("/static/app.js")

    @task(1)
    def load_manifest(self):
        self.client.get("/static/manifest.webmanifest")

    @task(1)
    def load_locale_en(self):
        self.client.get("/static/locales/en.json")

    @task(1)
    def load_locale_de(self):
        self.client.get("/static/locales/de.json")
