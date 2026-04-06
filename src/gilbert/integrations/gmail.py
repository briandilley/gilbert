"""Gmail email backend — EmailBackend implementation using Gmail API v1.

Uses Gilbert's GoogleService for authentication (service account with
domain-wide delegation). The ``email_address`` config field specifies
which mailbox to impersonate.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from gilbert.interfaces.email import EmailAddress, EmailAttachment, EmailBackend, EmailMessage

logger = logging.getLogger(__name__)

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailBackend(EmailBackend):
    """EmailBackend backed by Gmail API v1 via google-api-python-client."""

    def __init__(self, email_address: str) -> None:
        self._email_address = email_address
        self._service: Any = None  # gmail API resource

    async def initialize(self) -> None:
        # Actual service is built in InboxService.start() after GoogleService resolves
        pass

    def set_service(self, service: Any) -> None:
        """Set the authenticated Gmail API service resource.

        Called by InboxService after GoogleService builds the client.
        """
        self._service = service

    async def close(self) -> None:
        self._service = None

    def _ensure_service(self) -> Any:
        if self._service is None:
            raise RuntimeError("Gmail backend not initialized — call set_service() first")
        return self._service

    # --- Fetch ---

    async def list_message_ids(self, query: str = "", max_results: int = 100) -> list[str]:
        svc = self._ensure_service()
        q = query or "in:inbox OR in:sent"

        import asyncio

        ids: list[str] = []
        page_token: str | None = None

        while len(ids) < max_results:
            params: dict[str, Any] = {
                "userId": "me",
                "q": q,
                "maxResults": min(100, max_results - len(ids)),
            }
            if page_token:
                params["pageToken"] = page_token

            result = await asyncio.to_thread(
                svc.users().messages().list(**params).execute,
            )
            for m in result.get("messages", []):
                ids.append(m["id"])

            page_token = result.get("nextPageToken")
            if not page_token:
                break

        return ids

    async def get_message(self, message_id: str) -> EmailMessage | None:
        svc = self._ensure_service()

        import asyncio

        try:
            data = await asyncio.to_thread(
                svc.users().messages().get(userId="me", id=message_id, format="full").execute,
            )
        except Exception:
            logger.warning("Failed to fetch message %s", message_id, exc_info=True)
            return None

        headers = {
            h["name"].lower(): h["value"]
            for h in data.get("payload", {}).get("headers", [])
        }

        sender = _parse_sender(headers.get("from", ""))
        to = _parse_address_list(headers.get("to", ""))
        cc = _parse_address_list(headers.get("cc", ""))
        date = _parse_date(headers.get("date", ""))
        body_text, body_html = _extract_body(data.get("payload", {}))

        return EmailMessage(
            message_id=data["id"],
            thread_id=data.get("threadId", ""),
            subject=headers.get("subject", "(no subject)"),
            sender=sender,
            to=to,
            cc=cc,
            body_text=body_text,
            body_html=body_html,
            date=date,
            in_reply_to=headers.get("message-id", ""),
            headers=headers,
        )

    # --- Send ---

    async def send(
        self,
        to: list[EmailAddress],
        subject: str,
        body_html: str,
        body_text: str = "",
        cc: list[EmailAddress] | None = None,
        in_reply_to: str = "",
        thread_id: str = "",
        attachments: list[EmailAttachment] | None = None,
    ) -> str:
        svc = self._ensure_service()

        # If we have attachments, use mixed multipart with alternative body inside
        if attachments:
            msg = MIMEMultipart("mixed")
            body_part = MIMEMultipart("alternative")
            if body_text:
                body_part.attach(MIMEText(body_text, "plain"))
            body_part.attach(MIMEText(body_html, "html"))
            msg.attach(body_part)

            for att in attachments:
                from email.mime.application import MIMEApplication

                part = MIMEApplication(att.data, Name=att.filename)
                part["Content-Disposition"] = f'attachment; filename="{att.filename}"'
                if att.mime_type:
                    part.set_type(att.mime_type)
                msg.attach(part)
        else:
            msg = MIMEMultipart("alternative")
            if body_text:
                msg.attach(MIMEText(body_text, "plain"))
            msg.attach(MIMEText(body_html, "html"))

        msg["To"] = ", ".join(str(a) for a in to)
        msg["Subject"] = subject
        msg["From"] = self._email_address
        if cc:
            msg["Cc"] = ", ".join(str(a) for a in cc)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
            if not subject.startswith("Re:"):
                msg["Subject"] = f"Re: {subject}"

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        send_body: dict[str, str] = {"raw": raw}
        if thread_id:
            send_body["threadId"] = thread_id

        import asyncio

        result = await asyncio.to_thread(
            svc.users().messages().send(userId="me", body=send_body).execute,
        )
        return result.get("id", "")

    # --- Mark ---

    async def mark_read(self, message_id: str) -> None:
        svc = self._ensure_service()
        import asyncio

        await asyncio.to_thread(
            svc.users()
            .messages()
            .modify(userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]})
            .execute,
        )



# --- Helpers ---


def _parse_sender(from_header: str) -> EmailAddress:
    """Parse a From header into an EmailAddress."""
    match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>$', from_header.strip())
    if match:
        return EmailAddress(email=match.group(2).strip(), name=match.group(1).strip())
    return EmailAddress(email=from_header.strip().strip("<>"))


def _parse_address_list(header: str) -> list[EmailAddress]:
    """Parse a To/CC header into a list of EmailAddress."""
    if not header or not header.strip():
        return []

    addresses: list[EmailAddress] = []
    for part in header.split(","):
        part = part.strip()
        if not part:
            continue
        match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>$', part.strip())
        if match:
            addresses.append(EmailAddress(
                email=match.group(2).strip().lower(),
                name=match.group(1).strip(),
            ))
        elif "@" in part:
            addresses.append(EmailAddress(email=part.strip().lower()))
    return addresses


def _parse_date(date_str: str) -> datetime:
    """Best-effort parse of email Date header."""
    if not date_str:
        return datetime.now(timezone.utc)
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(date_str)
    except Exception:
        return datetime.now(timezone.utc)


def _extract_body(payload: dict[str, Any]) -> tuple[str, str]:
    """Extract (plain_text, html) from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""
        return text, ""

    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace") if data else ""
        stripped = re.sub(r"<[^>]+>", "", html)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        return stripped, html

    # Multipart
    parts = payload.get("parts", [])
    plain_text = ""
    html_text = ""

    for part in parts:
        part_mime = part.get("mimeType", "")

        if part_mime == "text/plain" and not plain_text:
            data = part.get("body", {}).get("data", "")
            if data:
                plain_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        elif part_mime == "text/html" and not html_text:
            data = part.get("body", {}).get("data", "")
            if data:
                html_text = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

        elif part_mime.startswith("multipart/"):
            nested_plain, nested_html = _extract_body(part)
            if nested_plain and not plain_text:
                plain_text = nested_plain
            if nested_html and not html_text:
                html_text = nested_html

    if not plain_text and html_text:
        plain_text = re.sub(r"<[^>]+>", "", html_text)
        plain_text = re.sub(r"\s+", " ", plain_text).strip()

    return plain_text, html_text
