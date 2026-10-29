"""Utilities for authorized sequential bulk login-email changes.

No inbox URL, email address, or OTP is logged by this module. OTP baseline values are
kept only as in-memory hashes so an old inbox code is not reused accidentally.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import aiohttp


MAX_LIST_BYTES = 2 * 1024 * 1024
MAX_INBOX_BYTES = 1024 * 1024
OTP_WAIT_SECONDS = 120
OTP_POLL_SECONDS = 5


class BulkInputError(ValueError):
    pass


class OtpLinkError(RuntimeError):
    pass


class OtpTimeoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmailTarget:
    email: str
    inbox_url: str


def _split_email_line(line: str) -> tuple[str, str]:
    for separator in ("|", "\t", ","):
        if separator in line:
            email, url = line.split(separator, 1)
            return email.strip(), url.strip()
    parts = line.split(None, 1)
    if len(parts) != 2:
        raise BulkInputError("Email line mein address aur OTP link dono hone chahiye")
    return parts[0].strip(), parts[1].strip()


def parse_email_links(text: str) -> list[EmailTarget]:
    targets: list[EmailTarget] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            email, url = _split_email_line(line)
        except BulkInputError as exc:
            raise BulkInputError(f"Email file line {line_number}: {exc}") from None
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise BulkInputError(f"Email file line {line_number}: email invalid hai")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise BulkInputError(f"Email file line {line_number}: OTP link invalid hai") from None
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise BulkInputError(f"Email file line {line_number}: OTP link invalid hai")
        targets.append(EmailTarget(email=email, inbox_url=url))
    if not targets:
        raise BulkInputError("Email file mein koi valid email+OTP link nahi mila")
    return targets


def otp_digest(code: str) -> str:
    return hashlib.sha256(code.encode("ascii")).hexdigest()


def _visible_text(raw: str) -> str:
    without_scripts = re.sub(
        r"(?is)<(script|style)\b[^>]*>.*?</\1>", " ", raw
    )
    without_tags = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
    return html.unescape(without_tags)


def extract_otp_candidates(raw: str, length: int | None = None) -> list[str]:
    """Return high-confidence candidates first, then conservative standalone codes."""
    visible = _visible_text(raw)
    sources = (raw, visible)
    found: list[tuple[int, int, str]] = []
    seen: set[tuple[int, str]] = set()

    def add(code: str, score: int, position: int) -> None:
        if length is not None and len(code) != length:
            return
        if length is None and not 4 <= len(code) <= 8:
            return
        key = (score // 10, code)
        if key not in seen:
            seen.add(key)
            found.append((score, position, code))

    keyed_patterns = (
        re.compile(
            r"(?i)(?:otp|passcode|verification[_ ]?code|login[_ ]?code|security[_ ]?code|code)"
            r"\s*[\"\']?\s*[:=]\s*[\"\']?(\d{4,8})\b"
        ),
        re.compile(
            r"(?i)(?:otp|passcode|verification\s+code|login\s+code|security\s+code|"
            r"telegram(?:\s+login)?\s+code|code)\s*(?:is|:|=|-)?\s*(\d{4,8})\b"
        ),
        re.compile(
            r"(?i)\b(\d{4,8})\b\s*(?:is\s+)?(?:your\s+)?"
            r"(?:otp|passcode|verification\s+code|login\s+code|security\s+code)\b"
        ),
    )
    for source in sources:
        for pattern in keyed_patterns:
            for match in pattern.finditer(source):
                add(match.group(1), 100, match.start())

    # Fallback for simple inbox pages that show only the code. Baseline collection
    # includes all standalone 4-8 digit values; after sending, the exact server length
    # is required, which greatly reduces false matches.
    generic = re.compile(
        rf"(?<!\d)(\d{{{length}}})(?!\d)" if length is not None else r"(?<!\d)(\d{4,8})(?!\d)"
    )
    for source in sources:
        for match in generic.finditer(source):
            code = match.group(1)
            if length == 4 and 2000 <= int(code) <= 2099:
                continue
            add(code, 20, match.start())

    found.sort(key=lambda item: (-item[0], item[1]))
    result: list[str] = []
    used: set[str] = set()
    for _, _, code in found:
        if code not in used:
            used.add(code)
            result.append(code)
    return result


def _validate_hostname(host: str, port: int) -> None:
    try:
        direct_ip = ipaddress.ip_address(host)
        addresses = [direct_ip]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        except (OSError, ValueError, IndexError):
            raise OtpLinkError("Inbox host resolve nahi hua") from None
    if not addresses or any(not address.is_global for address in addresses):
        raise OtpLinkError("Private/local inbox host allowed nahi hai")


async def validate_inbox_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise OtpLinkError("Inbox URL invalid hai")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    await asyncio.to_thread(_validate_hostname, parsed.hostname, port)


async def fetch_inbox_text(session: aiohttp.ClientSession, url: str) -> str:
    current_url = url
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        for _ in range(4):
            await validate_inbox_url(current_url)
            async with session.get(
                current_url,
                allow_redirects=False,
                timeout=timeout,
                headers={"User-Agent": "AuthorizedEmailVerifier/1.0"},
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise OtpLinkError("Inbox redirect invalid hai")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status != 200:
                    raise OtpLinkError(f"Inbox HTTP status {response.status}")
                content = await response.content.read(MAX_INBOX_BYTES + 1)
                if len(content) > MAX_INBOX_BYTES:
                    raise OtpLinkError("Inbox response size limit se zyada hai")
                encoding = response.charset or "utf-8"
                try:
                    return content.decode(encoding, errors="replace")
                except LookupError:
                    return content.decode("utf-8", errors="replace")
        raise OtpLinkError("Inbox redirect limit cross ho gayi")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise OtpLinkError("Inbox fetch failed") from None


async def read_otp_baseline(session: aiohttp.ClientSession, url: str) -> set[str]:
    raw = await fetch_inbox_text(session, url)
    return {otp_digest(code) for code in extract_otp_candidates(raw)}


async def wait_for_new_otp(
    session: aiohttp.ClientSession,
    url: str,
    baseline: set[str],
    length: int,
    timeout_seconds: int = OTP_WAIT_SECONDS,
) -> str:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise OtpTimeoutError("OTP 120 seconds ke andar nahi mila")
        try:
            raw = await asyncio.wait_for(
                fetch_inbox_text(session, url), timeout=min(15, remaining)
            )
            for code in extract_otp_candidates(raw, length=length):
                if otp_digest(code) not in baseline:
                    return code
        except (OtpLinkError, asyncio.TimeoutError):
            # A baseline was already obtained, so transient polling failures are safe
            # to retry until the two-minute deadline.
            pass
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise OtpTimeoutError("OTP 120 seconds ke andar nahi mila")
        await asyncio.sleep(min(OTP_POLL_SECONDS, remaining))
