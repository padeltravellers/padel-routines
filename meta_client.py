#!/usr/bin/env python3
"""
meta_client.py — base compartida para todos los scripts Meta Marketing API.

Carga credenciales de .env, inicializa el SDK, expone helpers de logging y
guardas de seguridad (confirmación requerida para ops destructivas/que mueven dinero).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount

ENV_PATH = Path(__file__).parent / ".env"
LOG_PATH = Path(__file__).parent / "output" / "meta_ops.log"


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)


def require(name: str) -> str:
    _load_env()
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Falta {name} en .env. Revisa la guía en meta_SETUP.md para obtenerlo."
        )
    return val


def log(event: str, **fields: Any) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().isoformat(timespec="seconds")
    payload = " ".join(f"{k}={v!r}" for k, v in fields.items())
    line = f"{ts} {event} {payload}".strip()
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")
    print(line, file=sys.stderr)


_api_initialized = False


def init_api() -> FacebookAdsApi:
    global _api_initialized
    _load_env()
    token = require("META_ACCESS_TOKEN")
    app_id = os.environ.get("META_APP_ID")
    app_secret = os.environ.get("META_APP_SECRET")
    if _api_initialized:
        return FacebookAdsApi.get_default_api()
    if app_id and app_secret:
        FacebookAdsApi.init(app_id=app_id, app_secret=app_secret, access_token=token)
    else:
        FacebookAdsApi.init(access_token=token)
    _api_initialized = True
    log("api_init", ad_account=os.environ.get("META_AD_ACCOUNT_ID"))
    return FacebookAdsApi.get_default_api()


def ad_account() -> AdAccount:
    init_api()
    acc_id = require("META_AD_ACCOUNT_ID")
    if not acc_id.startswith("act_"):
        acc_id = f"act_{acc_id}"
    return AdAccount(acc_id)


def ids() -> dict[str, str | None]:
    _load_env()
    return {
        "ad_account": os.environ.get("META_AD_ACCOUNT_ID"),
        "page": os.environ.get("META_PAGE_ID"),
        "ig_business": os.environ.get("META_INSTAGRAM_BUSINESS_ACCOUNT_ID"),
        "pixel": os.environ.get("META_PIXEL_ID"),
        "privacy_url": os.environ.get("META_PRIVACY_POLICY_URL"),
    }


def confirm(message: str, risky: bool = True) -> bool:
    """Gate para ops que mueven dinero o cambian estado. Entorno AUTO_CONFIRM=1 lo salta."""
    if not risky:
        return True
    if os.environ.get("META_AUTO_CONFIRM") == "1":
        log("confirm_auto", message=message)
        return True
    print(f"\n⚠️  CONFIRMACIÓN REQUERIDA: {message}")
    resp = input("Escribe 'si' para continuar: ").strip().lower()
    ok = resp in ("si", "sí", "y", "yes")
    log("confirm_result", message=message, ok=ok)
    return ok


def with_guard(risky: bool, description: str) -> Callable:
    """Decorator para marcar ops que requieren confirmación."""
    def wrap(fn: Callable) -> Callable:
        def inner(*args, **kwargs):
            if risky and not confirm(description):
                print("Cancelado por el usuario.")
                return None
            log("op_start", op=fn.__name__, description=description)
            result = fn(*args, **kwargs)
            log("op_done", op=fn.__name__)
            return result
        return inner
    return wrap


if __name__ == "__main__":
    init_api()
    acc = ad_account()
    info = acc.api_get(fields=["name", "currency", "account_status", "amount_spent", "balance"])
    print(f"✓ Conexión OK")
    print(f"  Cuenta: {info.get('name')} ({info.get('currency')})")
    print(f"  Estado: {info.get('account_status')} (1=activa)")
    print(f"  Gastado: {info.get('amount_spent')} | Balance: {info.get('balance')}")
    print(f"  IDs extra: {ids()}")
