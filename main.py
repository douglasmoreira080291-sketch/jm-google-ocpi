import os
import time
import base64
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Security
from fastapi.security import APIKeyHeader
from fastapi.responses import JSONResponse

app = FastAPI(title="JM Google OCPI Bridge", version="0.1.0")
authorization_header = APIKeyHeader(
    name="Authorization",
    scheme_name="OCPI_TOKEN_C",
    auto_error=False
)

TRICHARGE_BASE_URL = os.getenv("TRICHARGE_BASE_URL", "https://pay.tricharge.com.br/api/partner/v1").rstrip("/")
TRICHARGE_CLIENT_ID = os.getenv("TRICHARGE_CLIENT_ID", "")
TRICHARGE_CLIENT_SECRET = os.getenv("TRICHARGE_CLIENT_SECRET", "")
GOOGLE_OCPI_TOKEN = os.getenv("GOOGLE_OCPI_TOKEN", "")
OCPI_COUNTRY_CODE = os.getenv("OCPI_COUNTRY_CODE", "BR")
OCPI_PARTY_ID = os.getenv("OCPI_PARTY_ID", "JMC")
OCPI_OPERATOR_NAME = os.getenv("OCPI_OPERATOR_NAME", "JM Carregadores")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

_token_cache = {"token": None, "expires_at": 0.0}

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def ocpi_response(data: Any, status_code: int = 1000, status_message: str = "Success"):
    return {
        "data": data,
        "status_code": status_code,
        "status_message": status_message,
        "timestamp": now_iso(),
    }

def require_config():
    missing = [k for k, v in {
        "TRICHARGE_CLIENT_ID": TRICHARGE_CLIENT_ID,
        "TRICHARGE_CLIENT_SECRET": TRICHARGE_CLIENT_SECRET,
        "GOOGLE_OCPI_TOKEN": GOOGLE_OCPI_TOKEN,
    }.items() if not v]
    if missing:
        raise HTTPException(503, detail=f"Missing environment variables: {', '.join(missing)}")
def token_matches(header: Optional[str]) -> bool:
    if not header or not GOOGLE_OCPI_TOKEN:
        return False

    supplied = header.strip()

    if supplied.startswith("Token "):
        supplied = supplied[6:].strip()
    elif supplied.startswith("Bearer "):
        supplied = supplied[7:].strip()

    return supplied == GOOGLE_OCPI_TOKEN

def authorize(authorization: Optional[str]):
    require_config()
    if not token_matches(authorization):
        raise HTTPException(401, detail="Unauthorized")

async def tricharge_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 30:
        return _token_cache["token"]
    payload = {
        "grant_type": "client_credentials",
        "client_id": TRICHARGE_CLIENT_ID,
        "client_secret": TRICHARGE_CLIENT_SECRET,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{TRICHARGE_BASE_URL}/auth/token", json=payload)
        r.raise_for_status()
        body = r.json()
    token = body.get("access_token") or body.get("token") or body.get("data", {}).get("access_token")
    if not token:
        raise RuntimeError("TriCharge token response did not contain access_token")
    expires = int(body.get("expires_in", 900))
    _token_cache.update(token=token, expires_at=time.time() + expires)
    return token

async def tri_get(path: str, params: dict | None = None) -> Any:
    token = await tricharge_token()
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{TRICHARGE_BASE_URL}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        r.raise_for_status()
        return r.json()

def dig(obj: dict, *paths, default=None):
    for p in paths:
        cur = obj
        ok = True
        for key in p.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default

def map_status(conn: dict) -> str:
    raw = str(dig(conn, "ocpp_status", "status", default="")).strip().lower().replace("-", "_").replace(" ", "_")
    if conn.get("maintenance"):
        return "OUTOFORDER"
    mapping = {
        "available": "AVAILABLE",
        "preparing": "BLOCKED",
        "charging": "CHARGING",
        "suspendedev": "CHARGING",
        "suspended_ev": "CHARGING",
        "suspendedevse": "CHARGING",
        "suspended_evse": "CHARGING",
        "finishing": "CHARGING",
        "reserved": "RESERVED",
        "unavailable": "INOPERATIVE",
        "faulted": "OUTOFORDER",
        "occupied": "CHARGING",
        "offline": "UNKNOWN",
        "unknown": "UNKNOWN",
    }
    return mapping.get(raw, "UNKNOWN")

def connector_standard(value: Any) -> str:
    s = str(value or "").upper().replace("-", "").replace(" ", "")
    if "CCS2" in s or "COMBO2" in s:
        return "IEC_62196_T2_COMBO"
    if "CCS1" in s or "COMBO1" in s:
        return "IEC_62196_T1_COMBO"
    if "CHADEMO" in s:
        return "CHADEMO"
    if "TYPE2" in s or s in {"T2", "IEC62196T2"}:
        return "IEC_62196_T2"
    return "IEC_62196_T2_COMBO"

def station_list(body: Any) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("stations", "data", "items"):
            if isinstance(body.get(key), list):
                return body[key]
        if isinstance(body.get("station"), dict):
            return [body["station"]]
    return []

def connector_list(body: Any) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("connectors", "data", "items"):
            if isinstance(body.get(key), list):
                return body[key]
    return []

async def build_location(station: dict) -> dict:
    sid = str(dig(station, "id", "station_id", "cp_id", default="station"))
    try:
        conn_body = await tri_get(f"/stations/{sid}/connectors")
        connectors = connector_list(conn_body)
    except Exception:
        connectors = station.get("connectors", []) if isinstance(station.get("connectors"), list) else []

    lat = dig(station, "location.latitude", "latitude")
    lon = dig(station, "location.longitude", "longitude")
    if lat is None or lon is None:
        raise RuntimeError(f"Station {sid} has no coordinates")

    power_kw = float(dig(station, "hardware.power_kw", "power_kw", default=0) or 0)
    conn_type = dig(station, "hardware.connector_type", "connector_type", default="CCS2")
    evses = []
    for idx, c in enumerate(connectors or [{}], start=1):
        cid = str(dig(c, "connector_id", "id", default=idx))
        last = str(dig(c, "status_at", "last_updated", default=now_iso()))
        evses.append({
            "uid": f"{sid}-{cid}"[:36],
            "evse_id": f"{OCPI_COUNTRY_CODE}*{OCPI_PARTY_ID}*E{sid}{cid}"[:48],
            "status": map_status(c),
            "connectors": [{
                "id": cid[:36],
                "standard": connector_standard(dig(c, "connector_type", default=conn_type)),
                "format": "CABLE",
                "power_type": "DC",
                "max_voltage": 1000,
                "max_amperage": 130,
                "max_electric_power": int(power_kw * 1000) if power_kw else None,
                "last_updated": last,
            }],
            "last_updated": last,
        })

    # Remove optional null values.
    for e in evses:
        e["connectors"][0] = {k:v for k,v in e["connectors"][0].items() if v is not None}

    last_updated = max([e["last_updated"] for e in evses], default=now_iso())
    city = str(dig(station, "location.city", "city", default="Sete Lagoas"))
    address = str(dig(station, "location.address", "address", default="Rua Felipe Chamon, 689"))
    postal = str(dig(station, "location.postal_code", "postal_code", default="35700-000"))

    return {
        "country_code": OCPI_COUNTRY_CODE,
        "party_id": OCPI_PARTY_ID,
        "id": sid[:36],
        "publish": True,
        "name": str(dig(station, "name", default=OCPI_OPERATOR_NAME))[:100],
        "address": address[:45],
        "city": city[:45],
        "postal_code": postal[:10],
        "country": "BRA",
        "coordinates": {"latitude": str(lat), "longitude": str(lon)},
        "evses": evses,
        "operator": {"name": OCPI_OPERATOR_NAME},
        "time_zone": "America/Sao_Paulo",
        "last_updated": last_updated,
    }

@app.get("/")
async def root():
    return {"service": "JM Google OCPI Bridge", "status": "ok"}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "tricharge_client_id_loaded": bool(TRICHARGE_CLIENT_ID),
        "tricharge_client_secret_loaded": bool(TRICHARGE_CLIENT_SECRET),
        "google_ocpi_token_loaded": bool(GOOGLE_OCPI_TOKEN),
        "google_ocpi_token_length": len(GOOGLE_OCPI_TOKEN)
    }
@app.get("/auth-test")
async def auth_test(authorization: Optional[str] = Security(authorization_header)):
    if not authorization:
        return {
            "authorization_received": False
        }

    if authorization.startswith("Token "):
        supplied = authorization[6:].strip()
        scheme = "Token"
    elif authorization.startswith("Bearer "):
        supplied = authorization[7:].strip()
        scheme = "Bearer"
    else:
        supplied = authorization.strip()
        scheme = "Other"

    return {
        "authorization_received": True,
        "scheme": scheme,
        "supplied_length": len(supplied),
        "expected_length": len(GOOGLE_OCPI_TOKEN),
        "matches": supplied == GOOGLE_OCPI_TOKEN
    }

@app.get("/ocpi/versions")
async def versions(authorization: Optional[str] = Header(None)):
    authorize(authorization)
    base = PUBLIC_BASE_URL or ""
    return ocpi_response([{"version": "2.2.1", "url": f"{base}/ocpi/cpo/2.2.1"}])

@app.get("/ocpi/cpo/2.2.1")
async def version_details(authorization: Optional[str] = Header(None)):
    authorize(authorization)
    base = PUBLIC_BASE_URL or ""
    return ocpi_response({
        "version": "2.2.1",
        "endpoints": [{"identifier": "locations", "role": "SENDER", "url": f"{base}/ocpi/cpo/2.2.1/locations"}],
    })

@app.get("/ocpi/cpo/2.2.1/locations")
async def locations(
    authorization: Optional[str] = Security(authorization_header),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
):
    authorize(authorization)
    try:
        body = await tri_get("/stations", params={"include": "connectors"})
        stations = station_list(body)
        locations = [await build_location(s) for s in stations]
        # Small deployment: support offset/limit even if Google is configured as Pagination=None.
        page = locations[offset:offset + limit]
        return JSONResponse(content=ocpi_response(page), headers={
            "X-Total-Count": str(len(locations)),
            "X-Limit": str(limit),
        })
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, detail=f"TriCharge API returned HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(502, detail=f"Bridge error: {e}")

@app.get("/ocpi/cpo/2.2.1/locations/{location_id}")
async def location(location_id: str, authorization: Optional[str] = Header(None)):
    authorize(authorization)
    try:
        body = await tri_get(f"/stations/{location_id}", params={"include": "connectors"})
        stations = station_list(body)
        station = stations[0] if stations else body.get("station", body) if isinstance(body, dict) else None
        if not isinstance(station, dict):
            raise HTTPException(404, detail="Location not found")
        return ocpi_response(await build_location(station))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, detail=f"Bridge error: {e}")
