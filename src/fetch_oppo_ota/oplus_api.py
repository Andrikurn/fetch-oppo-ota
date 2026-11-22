"""Direct async client for OPPO / Oplus official OTA servers (update/v3).

Communicates directly with OPPO's regional ``component-otapc-*.allawnos.com``
(and ``allawntech.com`` for CN) endpoints using AES-256-CTR payload encryption
and RSA-OAEP-SHA1 key wrapping.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
import re
import time
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import httpx

from .models import UpdateData


class OplusOtaError(RuntimeError):
    """Raised when an OPPO/Oplus direct OTA query fails."""


@dataclass(frozen=True, slots=True)
class OplusServerParams:
    v5_url: str
    v3_url: str
    public_key_b64: str
    negotiation_version: str
    default_nv_carrier: str


OPLUS_SERVERS: dict[str, OplusServerParams] = {
    "GL": OplusServerParams(
        v5_url="https://component-ota-sg.allawnos.com/update/v5",
        v3_url="https://component-otapc-sg.allawnos.com/update/v3",
        public_key_b64=(
            "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAkA980wxi+eTGcFDiw2I6"
            "RrUeO4jL/Aj3Yw4dNuW7tYt+O1sRTHgrzxPD9SrOqzz7G0KgoSfdFHe3JVLPN+U1"
            "waK+T0HfLusVJshDaMrMiQFDUiKajb+QKr+bXQhVofH74fjat+oRJ8vjXARSpFk4"
            "/41x5j1Bt/2bHoqtdGPcUizZ4whMwzap+hzVlZgs7BNfepo24PWPRujsN3uopl+8"
            "u4HFpQDlQl7GdqDYDj2zNOHdFQI2UpSf0aIeKCKOpSKF72KDEESpJVQsqO4nxMwE"
            "i2jMujQeCHyTCjBZ+W35RzwT9+0pyZv8FB3c7FYY9FdF/+lvfax5mvFEBd9jO+dp"
            "MQIDAQAB"
        ),
        negotiation_version="1615895993238",
        default_nv_carrier="00011011",
    ),
    "EU": OplusServerParams(
        v5_url="https://component-ota-eu.allawnos.com/update/v5",
        v3_url="https://component-otapc-eu.allawnos.com/update/v3",
        public_key_b64=(
            "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAh8/EThsK3f0WyyPgrtXb"
            "/D0Xni6UZNppaQHUqHWo976cybl92VxmehE0ISObnxERaOtrlYmTPIxkVC9MMueD"
            "vTwZ1l0KxevZVKU0sJRxNR9AFcw6D7k9fPzzpNJmhSlhpNbt3BEepdgibdRZbacF"
            "3NWy3ejOYWHgxC+I/Vj1v7QU5gD+1OhgWeRDcwuV4nGY1ln2lvkRj8EiJYXfkSq/"
            "wUI5AvPdNXdEqwou4FBcf6mD84G8pKDyNTQwwuk9lvFlcq4mRqgYaFg9DAgpDgqV"
            "K4NTJWM7tQS1GZuRA6PhupfDqnQExyBFhzCefHkEhcFywNyxlPe953NWLFWwbGvF"
            "KwIDAQAB"
        ),
        negotiation_version="1615897067573",
        default_nv_carrier="01000100",
    ),
    "IN": OplusServerParams(
        v5_url="https://component-ota-in.allawnos.com/update/v5",
        v3_url="https://component-otapc-in.allawnos.com/update/v3",
        public_key_b64=(
            "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwYtghkzeStC9YvAwOQmW"
            "ylbp74Tj8hhi3f9IlK7A/CWrGbLgzz/BeKxNb45zBN8pgaaEOwAJ1qZQV5G4nPro"
            "WCPOP1ro1PkemFJvw/vzOOT5uN0ADnHDzZkZXCU/knxqUSfLcwQlHXsYhNsAm7uO"
            "KjY9YXF4zWzYN0eFPkML3Pj/zg7hl/ov9clB2VeyI1/blMHFfcNA/fvqDTENXcNB"
            "IhgJvXiCpLcZqp+aLZPC5AwY/sCb3j5jTWer0Rk0ZjQBZE1AncwYvUx4mA65U59c"
            "WpTyl4c47J29MsQ66hqWv6eBHlDNZSEsQpHePUqgsf7lmO5Wd7teB8ugQki2oz1Y"
            "5QIDAQAB"
        ),
        negotiation_version="1615896309308",
        default_nv_carrier="00011011",
    ),
    "CN": OplusServerParams(
        v5_url="https://component-ota-cn.allawntech.com/update/v5",
        v3_url="https://component-otapc-cn.allawntech.com/update/v3",
        public_key_b64=(
            "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEApXYGXQpNL7gmMzzvajHa"
            "oZIHQQvBc2cOEhJc7/tsaO4sT0unoQnwQKfNQCuv7qC1Nu32eCLuewe9LSYhDXr9"
            "KSBWjOcCFXVXteLO9WCaAh5hwnUoP/5/Wz0jJwBA+yqs3AaGLA9wJ0+B2lB1vLE4"
            "FZNE7exUfwUc03fJxHG9nCLKjIZlrnAAHjRCd8mpnADwfkCEIPIGhnwq7pdkbamZ"
            "coZfZud1+fPsELviB9u447C6bKnTU4AaMcR9Y2/uI6TJUTcgyCp+ilgU0JxemrSI"
            "PFk3jbCbzamQ6Shkw/jDRzYoXpBRg/2QDkbq+j3ljInu0RHDfOeXf3VBfHSnQ66H"
            "CwIDAQAB"
        ),
        negotiation_version="1615879139745",
        default_nv_carrier="10010111",
    ),
}

DEFAULT_IMEI = "000000000000000"
ZERO_TAIL = "0001_000000000001"


def _aes_ctr_crypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    cryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    return cryptor.update(data) + cryptor.finalize()


def _zero_tail_ota_version(ota_version: str) -> str:
    if ota_version.endswith(ZERO_TAIL):
        return ota_version
    match = re.fullmatch(r"(?P<prefix>.+_)\d{4}_\d{12}", ota_version)
    if match:
        return f"{match.group('prefix')}{ZERO_TAIL}"
    return ota_version


class OplusOtaClient:
    """Async client for querying OPPO/Oplus official OTA servers directly."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=15.0),
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OplusOtaClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def query_latest_ota(
        self,
        *,
        model: str,
        region: str = "GL",
        ota_version: str | None = None,
        coloros_major: int = 15,
        nv_id: str | None = None,
        beta: bool = False,
    ) -> UpdateData:
        """Query OPPO's official update/v3 endpoint and return an ``UpdateData``."""
        reg = region.strip().upper()
        if reg not in OPLUS_SERVERS:
            raise OplusOtaError(
                f"Unsupported region {region!r} (choose from GL, EU, IN, CN)"
            )
        if not (11 <= coloros_major <= 16):
            raise OplusOtaError("ColorOS major version must be between 11 and 16.")

        clean_model = model.strip().upper()
        if not clean_model:
            raise OplusOtaError("Device model number is required (e.g. CPH2791).")

        # If user didn't supply an OTA version, construct a generic base version
        base_ota = (
            ota_version.strip()
            if ota_version and ota_version.strip()
            else f"{clean_model}_11.A.01_{ZERO_TAIL}"
        )

        attempts = [base_ota]
        fallback = _zero_tail_ota_version(base_ota)
        if fallback != base_ota:
            attempts.append(fallback)

        errors: list[str] = []
        # Try mobile update/v5 first, then PC update/v3 as fallback
        for use_v5 in (True, False):
            for candidate_ota in attempts:
                try:
                    return await self._send_query(
                        model=clean_model,
                        region=reg,
                        ota_version=candidate_ota,
                        coloros_major=coloros_major,
                        nv_id=nv_id,
                        beta=beta,
                        use_v5=use_v5,
                    )
                except (httpx.HTTPError, OplusOtaError) as exc:
                    tag = "v5" if use_v5 else "v3"
                    errors.append(f"[{tag}] {candidate_ota}: {exc}")

        raise OplusOtaError(" | ".join(errors))

    async def _send_query(
        self,
        *,
        model: str,
        region: str,
        ota_version: str,
        coloros_major: int,
        nv_id: str | None,
        beta: bool,
        use_v5: bool = True,
    ) -> UpdateData:
        server = OPLUS_SERVERS[region]
        now_ms = int(time.time() * 1000)
        rui_version = coloros_major - 9
        language = "zh-CN" if region == "CN" else "en-EN"
        nv_carrier = (
            nv_id.strip()
            if nv_id and nv_id.strip() and nv_id.strip() != "0"
            else server.default_nv_carrier
        )
        ota_prefix = "_".join(ota_version.split("_")[:2])
        device_id = hashlib.sha256(DEFAULT_IMEI.encode("utf-8")).hexdigest().upper()
        android_version = f"Android{9 + rui_version}.0"
        coloros_version = f"ColorOS{coloros_major}"

        plain_body = {
            "language": language,
            "romVersion": ota_prefix,
            "otaVersion": ota_version,
            "androidVersion": android_version,
            "colorOSVersion": coloros_version,
            "model": model,
            "productName": model,
            "operator": "unknown",
            "uRegion": region,
            "trackRegion": region,
            "imei": DEFAULT_IMEI,
            "imei1": DEFAULT_IMEI,
            "mode": "1" if beta else "0",
            "registrationId": "unknown",
            "deviceId": device_id,
            "version": "2" if use_v5 else "3",
            "type": "1",
            "otaPrefix": ota_prefix,
            "isRealme": "1" if "RMX" in model else "0",
            "time": str(now_ms),
            "canCheckSelf": "0",
        }
        plain_json = json.dumps(plain_body, separators=(",", ":"), ensure_ascii=False)

        # Encrypt request body with ephemeral AES-256-CTR key
        aes_key = os.urandom(32)
        iv = os.urandom(16)
        cipher_bytes = _aes_ctr_crypt(plain_json.encode("utf-8"), aes_key, iv)
        aes_key_b64 = base64.b64encode(aes_key).decode("ascii")

        cipher_iv_json = json.dumps(
            {
                "cipher": base64.b64encode(cipher_bytes).decode("ascii"),
                "iv": base64.b64encode(iv).decode("ascii"),
            },
            separators=(",", ":"),
        )
        outer_body = json.dumps({"params": cipher_iv_json}, separators=(",", ":"))

        # Wrap the base64 AES key using the regional Oplus RSA public key
        public_key = serialization.load_der_public_key(
            base64.b64decode(server.public_key_b64)
        )
        protected_key = public_key.encrypt(
            aes_key_b64.encode("utf-8"),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA1()),
                algorithm=hashes.SHA1(),
                label=None,
            ),
        )
        protected_header = json.dumps(
            {
                "SCENE_1": {
                    "protectedKey": base64.b64encode(protected_key).decode("ascii"),
                    "version": str(now_ms + 86_400_000),
                    "negotiationVersion": server.negotiation_version,
                }
            },
            separators=(",", ":"),
        )

        headers = {
            "language": language,
            "romVersion": ota_prefix,
            "otaVersion": ota_version,
            "androidVersion": android_version,
            "colorOSVersion": coloros_version,
            "model": model,
            "infVersion": "1",
            "operator": "unknown",
            "nvCarrier": nv_carrier,
            "uRegion": region,
            "trackRegion": region,
            "imei": DEFAULT_IMEI,
            "imei1": DEFAULT_IMEI,
            "deviceId": device_id,
            "mode": "client_auto",
            "version": "2",
            "protectedKey": protected_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "NULL",
        }
        if not use_v5:
            headers["channel"] = "pc"

        endpoint_url = server.v5_url if use_v5 else server.v3_url
        resp = await self._client.post(endpoint_url, headers=headers, content=outer_body)
        return self._decode_response(resp, aes_key)

    def _decode_response(self, resp: httpx.Response, aes_key: bytes) -> UpdateData:
        try:
            envelope = resp.json()
        except ValueError as exc:
            raise OplusOtaError(
                f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:200]}"
            ) from exc

        code = int(envelope.get("responseCode") or 200)
        if resp.status_code >= 400 or code != 200:
            msg = envelope.get("errMsg") or resp.reason_phrase or "Unknown error"
            raise OplusOtaError(f"HTTP {resp.status_code}, responseCode={code}: {msg}")

        body = envelope.get("body")
        if isinstance(body, str):
            payload_obj = json.loads(body)
        elif isinstance(body, dict):
            payload_obj = body
        else:
            raise OplusOtaError("Missing or invalid encrypted body in server response.")

        cipher_bytes = base64.b64decode(payload_obj["cipher"])
        response_iv = base64.b64decode(payload_obj["iv"])
        plain_bytes = _aes_ctr_crypt(cipher_bytes, aes_key, response_iv)
        decoded = json.loads(plain_bytes.decode("utf-8"))

        if decoded.get("checkFailReason"):
            raise OplusOtaError(
                f"No OTA returned by server: {decoded['checkFailReason']}"
            )

        # Pick the largest component packet (the main Full OTA zip)
        best_packet: dict[str, Any] | None = None
        best_size = -1
        for comp in decoded.get("components") or []:
            pkt = comp.get("componentPackets") or {}
            url = str(pkt.get("url") or pkt.get("manualUrl") or "").strip()
            if not url:
                continue
            size = int(pkt.get("size") or 0)
            if size >= best_size:
                best_size = size
                best_packet = pkt

        if not best_packet:
            raise OplusOtaError("Server returned no downloadable OTA packets.")

        download_url = str(
            best_packet.get("url") or best_packet.get("manualUrl") or ""
        ).strip()
        version_name = decoded.get("versionName") or decoded.get("realOtaVersion")
        real_ota = decoded.get("realOtaVersion") or version_name
        os_ver = decoded.get("realOsVersion") or decoded.get("osVersion") or ""
        android_ver = (
            decoded.get("realAndroidVersion") or decoded.get("androidVersion") or ""
        )
        sec_patch = decoded.get("securityPatch") or ""

        desc_parts = [p for p in (os_ver, android_ver, f"Security Patch: {sec_patch}" if sec_patch else "") if p]
        return UpdateData(
            id=None,
            version_number=version_name,
            ota_version_number=real_ota,
            changelog="\n".join(desc_parts) if desc_parts else None,
            description=None,
            download_url=download_url,
            download_size=int(best_packet.get("size") or 0),
            filename=f"{version_name}.zip" if version_name else None,
            md5sum=best_packet.get("md5"),
        )
