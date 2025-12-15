# sigex_client.py
import os
import requests


class SigexNotConfigured(RuntimeError):
    """SIGEX mTLS / env vars not set."""
    pass


def _base_url() -> str:
    return os.getenv("SIGEX_BASE_URL", "https://sigex.kz:10443").rstrip("/")


def _cert_tuple():
    crt = (os.getenv("SIGEX_MTLS_CRT") or "").strip()
    key = (os.getenv("SIGEX_MTLS_KEY") or "").strip()
    if not crt or not key:
        raise SigexNotConfigured(
            "SIGEX не настроен: задайте SIGEX_MTLS_CRT и SIGEX_MTLS_KEY (пути к mTLS cert/key)."
        )
    return (crt, key)


def sigex_post_json(path: str, payload: dict, params: dict | None = None) -> dict:
    """
    POST JSON -> JSON
    """
    url = f"{_base_url()}{path}"
    r = requests.post(url, json=payload, params=params, cert=_cert_tuple(), timeout=60)
    r.raise_for_status()
    return r.json()


def sigex_get_json(path: str, params: dict | None = None) -> dict:
    """
    GET -> JSON
    """
    url = f"{_base_url()}{path}"
    r = requests.get(url, params=params, cert=_cert_tuple(), timeout=60)
    r.raise_for_status()
    return r.json()


def sigex_post_octet(path: str, data_bytes: bytes, params: dict | None = None) -> dict:
    """
    POST application/octet-stream -> JSON (если вернёт)
    """
    url = f"{_base_url()}{path}"
    headers = {"Content-Type": "application/octet-stream"}
    r = requests.post(url, params=params, data=data_bytes, headers=headers, cert=_cert_tuple(), timeout=120)
    r.raise_for_status()
    # иногда API может вернуть пустое тело
    if not r.content:
        return {}
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}
