"""
news_engine.py
--------------
Filtro de noticias de alto impacto para TradingProEA.

Objetivo: evitar que el bot EJECUTE operaciones durante ventanas de
alto impacto (NFP, CPI, FOMC/Powell, PPI, PIB, etc.) usando el
calendario económico de Finnhub.

Uso típico dentro de bot_engine.py, justo antes de mandar la orden a MT5:

    from news_engine import is_news_blackout

    blocked, evento = is_news_blackout()
    if blocked:
        log(f"[NEWS] Orden bloqueada por evento de alto impacto: {evento['event']} "
            f"({evento['minutes_to_event']} min)")
        continue  # no ejecutar esta señal

No bloquea la GENERACIÓN de señales (signal_engine.py sigue publicando
normalmente en Telegram) — solo bloquea la EJECUCIÓN en bot_engine.py,
según lo acordado.

Requiere en el .env:
    FINNHUB_API_KEY=tu_api_key_aqui
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # si ya cargas el .env en otro punto del sistema, no pasa nada

# ------------------------------------------------------------------
# CONFIGURACIÓN
# ------------------------------------------------------------------

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/economic"

# Ventana de bloqueo alrededor del evento (minutos)
NEWS_BLACKOUT_BEFORE_MIN = int(os.getenv("NEWS_BLACKOUT_BEFORE_MIN", "30"))
NEWS_BLACKOUT_AFTER_MIN = int(os.getenv("NEWS_BLACKOUT_AFTER_MIN", "30"))

# Solo bloquear por estos niveles de impacto (Finnhub usa 1=bajo, 2=medio, 3=alto)
NEWS_MIN_IMPACT = int(os.getenv("NEWS_MIN_IMPACT", "3"))

# Solo eventos de este país (el oro reacciona principalmente a USD)
NEWS_COUNTRY = os.getenv("NEWS_COUNTRY", "US")

# Cada cuánto se refresca el calendario desde Finnhub (evita gastar rate limit)
CACHE_REFRESH_MIN = int(os.getenv("NEWS_CACHE_REFRESH_MIN", "15"))

# Cuántos días hacia adelante se piden en cada refresh
CALENDAR_LOOKAHEAD_DAYS = int(os.getenv("NEWS_CALENDAR_LOOKAHEAD_DAYS", "2"))

logger = logging.getLogger("news_engine")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] [NEWS] %(message)s", "%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ------------------------------------------------------------------
# CACHE EN MEMORIA
# ------------------------------------------------------------------

_cache = {
    "events": [],       # lista de eventos normalizados
    "fetched_at": None, # datetime UTC del último fetch exitoso
    "last_error": None, # último error de API, si lo hubo (para diagnóstico)
}


def _fetch_calendar_from_finnhub():
    """Trae el calendario económico crudo de Finnhub. Puede lanzar excepción."""
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY no configurada en el .env")

    today = datetime.now(timezone.utc).date()
    to_date = today + timedelta(days=CALENDAR_LOOKAHEAD_DAYS)

    params = {
        "from": today.isoformat(),
        "to": to_date.isoformat(),
        "token": FINNHUB_API_KEY,
    }

    resp = requests.get(FINNHUB_URL, params=params, timeout=10)

    if resp.status_code == 403:
        raise PermissionError(
            "Finnhub devolvió 403 en /calendar/economic — ese endpoint puede "
            "requerir plan pago en tu cuenta. Revisa finnhub.io/pricing."
        )
    resp.raise_for_status()

    data = resp.json()
    raw_events = data.get("economicCalendar", data if isinstance(data, list) else [])

    events = []
    for ev in raw_events:
        try:
            country = ev.get("country", "")
            impact = ev.get("impact", 0)
            event_time_str = ev.get("time")  # formato típico: "2026-08-19 08:30:00"
            if not event_time_str:
                continue

            event_time = datetime.strptime(event_time_str, "%Y-%m-%d %H:%M:%S")
            event_time = event_time.replace(tzinfo=timezone.utc)

            events.append({
                "event": ev.get("event", "Evento sin nombre"),
                "country": country,
                "impact": impact,  # 1=bajo, 2=medio, 3=alto (según Finnhub)
                "time_utc": event_time,
            })
        except (ValueError, TypeError):
            continue  # evento con formato inesperado, se ignora

    return events


def _refresh_cache_if_needed(force=False):
    now = datetime.now(timezone.utc)
    needs_refresh = (
        force
        or _cache["fetched_at"] is None
        or (now - _cache["fetched_at"]) > timedelta(minutes=CACHE_REFRESH_MIN)
    )
    if not needs_refresh:
        return

    try:
        events = _fetch_calendar_from_finnhub()
        _cache["events"] = events
        _cache["fetched_at"] = now
        _cache["last_error"] = None
        logger.info(f"Calendario actualizado — {len(events)} eventos cargados.")
    except Exception as e:
        _cache["last_error"] = str(e)
        # Fail-open: si Finnhub falla (403, timeout, rate limit, etc.) NO se
        # bloquea el bot indefinidamente. Se loguea fuerte para que se note,
        # pero se sigue operando con la última data buena que haya en cache.
        logger.warning(f"No se pudo refrescar el calendario ({e}). "
                        f"Usando cache anterior ({len(_cache['events'])} eventos).")


def is_news_blackout(now=None):
    """
    Devuelve (bloqueado: bool, evento: dict|None).

    bloqueado=True si `now` cae dentro de la ventana
    [evento - NEWS_BLACKOUT_BEFORE_MIN, evento + NEWS_BLACKOUT_AFTER_MIN]
    de algún evento de impacto >= NEWS_MIN_IMPACT en NEWS_COUNTRY.
    """
    _refresh_cache_if_needed()

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    for ev in _cache["events"]:
        if ev["country"] != NEWS_COUNTRY:
            continue
        if ev["impact"] < NEWS_MIN_IMPACT:
            continue

        window_start = ev["time_utc"] - timedelta(minutes=NEWS_BLACKOUT_BEFORE_MIN)
        window_end = ev["time_utc"] + timedelta(minutes=NEWS_BLACKOUT_AFTER_MIN)

        if window_start <= now <= window_end:
            minutes_to_event = round((ev["time_utc"] - now).total_seconds() / 60, 1)
            return True, {
                "event": ev["event"],
                "country": ev["country"],
                "impact": ev["impact"],
                "time_utc": ev["time_utc"].isoformat(),
                "minutes_to_event": minutes_to_event,
            }

    return False, None


def next_high_impact_event():
    """Devuelve el próximo evento de alto impacto (o None), útil para dashboards/logs."""
    _refresh_cache_if_needed()
    now = datetime.now(timezone.utc)

    upcoming = [
        ev for ev in _cache["events"]
        if ev["country"] == NEWS_COUNTRY
        and ev["impact"] >= NEWS_MIN_IMPACT
        and ev["time_utc"] >= now
    ]
    if not upcoming:
        return None

    upcoming.sort(key=lambda e: e["time_utc"])
    nearest = upcoming[0]
    return {
        "event": nearest["event"],
        "time_utc": nearest["time_utc"].isoformat(),
        "minutes_away": round((nearest["time_utc"] - now).total_seconds() / 60, 1),
    }


# ------------------------------------------------------------------
# PRUEBA MANUAL: python news_engine.py
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("Probando news_engine.py...\n")
    blocked, event = is_news_blackout()
    if blocked:
        print(f"BLOQUEADO ahora mismo por: {event['event']} "
              f"({event['country']}, impacto {event['impact']}, "
              f"faltan {event['minutes_to_event']} min)")
    else:
        print("Sin bloqueo de noticias en este momento.")

    nxt = next_high_impact_event()
    if nxt:
        print(f"\nPróximo evento de alto impacto: {nxt['event']} "
              f"en {nxt['minutes_away']} min ({nxt['time_utc']})")
    else:
        print("\nNo hay eventos de alto impacto próximos en la ventana consultada.")

    if _cache["last_error"]:
        print(f"\n[AVISO] Último error de Finnhub: {_cache['last_error']}")
