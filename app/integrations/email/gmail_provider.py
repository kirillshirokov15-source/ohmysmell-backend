import asyncio
import base64
import hashlib
import os
import re
from datetime import datetime, timezone
from email.utils import parseaddr
from html.parser import HTMLParser
from app.config.settings import settings
from app.integrations.email.gmail_auth import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPES,
    load_gmail_credentials,
)
from app.integrations.email.provider import EmailFetchBatch, EmailMessage


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(
            line.strip()
            for line in "".join(self.parts).splitlines()
            if line.strip()
        )


class ReadOnlyHttp:
    """Gmail API transport cannot send/modify/delete, even via an accidental SDK call.
    OAuth refresh runs inside AuthorizedHttp, independently of Gmail operations.
    """
    def __init__(self, http):
        self.http = http

    def request(self, uri, method="GET", *args, **kwargs):
        if method.upper() != "GET":
            raise RuntimeError("Gmail transport permits GET only")
        return self.http.request(uri, method, *args, **kwargs)

    def close(self):
        self.http.close()


class GmailEmailProvider:
    def __init__(self, service=None, *, allowed_message_ids=None, order_query=None) -> None:
        self.service = service
        self._query_acknowledged = set()
        self.order_query = (os.getenv("GMAIL_ORDER_QUERY", "") if order_query is None else order_query).strip()
        if len(self.order_query) > 2000 or any(ord(c) < 32 for c in self.order_query):
            raise ValueError("Invalid Gmail order query")
        values = allowed_message_ids if allowed_message_ids is not None else os.getenv("GMAIL_ALLOWED_MESSAGE_IDS", "").split(",")
        self.allowed_message_ids = tuple(sorted({v.strip() for v in values if v.strip()}))
        if any(not re.fullmatch(r"[a-fA-F0-9]{1,64}", value) for value in self.allowed_message_ids):
            raise ValueError("Invalid Gmail message selector")

    def cursor_key(self) -> str:
        """Scope cursors to authenticated account AND selector, without logging email."""
        if self.service is None:
            self.service = self._build_service()
        account = self.service.users().getProfile(userId=settings.gmail_user_id).execute()["emailAddress"].strip().casefold()
        scope = account + "\n" + (",".join(self.allowed_message_ids) or "inbox:" + settings.gmail_initial_query)
        if self.order_query:
            scope += "\norder-query:" + self.order_query
        return "gmail:" + hashlib.sha256(scope.encode()).hexdigest()[:40]

    async def fetch_unprocessed(
        self, cursor: str | None = None
    ) -> EmailFetchBatch:
        return await asyncio.to_thread(self._fetch_sync, cursor)

    def _fetch_sync(self, cursor: str | None) -> EmailFetchBatch:
        if self.service is None:
            self.service = self._build_service()
        service = self.service
        if cursor:
            try:
                message_ids, next_cursor = self._message_ids_from_history(
                    service, cursor
                )
            except Exception as error:
                if getattr(getattr(error, "resp", None), "status", None) != 404:
                    raise
                message_ids, next_cursor = self._bootstrap(service, recovery=True)
        else:
            message_ids, next_cursor = self._bootstrap(service)
        if self.allowed_message_ids:
            message_ids = [value for value in message_ids if value in self.allowed_message_ids]
        if self.order_query:
            selected, _ = self._initial_message_ids(service, query=self._order_query())
            if self.allowed_message_ids:
                selected = [value for value in selected if value in self.allowed_message_ids]
            self._query_acknowledged.intersection_update(selected)
            # Reconcile the selected IDs every poll: search indexing and labels may
            # lag behind history. Only successful, committed messages are cached;
            # restart safely replays through durable application deduplication.
            message_ids = [value for value in selected if value not in self._query_acknowledged]
        messages = [
            message
            for message_id in message_ids
            if (message := self._get_message(service, message_id)) is not None
        ]
        if next_cursor is None:
            profile = service.users().getProfile(
                userId=settings.gmail_user_id
            ).execute()
            next_cursor = profile.get("historyId")
        return EmailFetchBatch(messages=messages, next_cursor=next_cursor)

    def _bootstrap(self, service, *, recovery=False) -> tuple[list[str], str | None]:
        # Capture the cursor before listing. Messages arriving during the list
        # are then visible through history on the next poll (duplicates remain
        # harmless because external_message_id is unique).
        profile = service.users().getProfile(
            userId=settings.gmail_user_id
        ).execute()
        # After history expiration, unread-only bootstrap can lose messages read
        # by the owner during downtime. Rescan INBOX; durable IDs deduplicate it.
        if self.allowed_message_ids:
            message_ids = list(self.allowed_message_ids)
        else:
            message_ids, _ = self._initial_message_ids(service, query=self._order_query() if self.order_query else ("in:inbox" if recovery else None))
        return message_ids, profile.get("historyId")

    def _order_query(self):
        # A caller-supplied OR must not escape the INBOX boundary.
        return "in:inbox (" + self.order_query + ")"

    def acknowledge(self, message_id: str) -> None:
        if self.order_query:
            self._query_acknowledged.add(message_id)

    def _build_service(self):
        from googleapiclient.discovery import build

        credentials = load_gmail_credentials(allow_interactive=False)
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
        from app.integrations.http_tls import verified_ca_bundle
        http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=20, ca_certs=verified_ca_bundle()))
        return build("gmail", "v1", http=ReadOnlyHttp(http), cache_discovery=False)

    def readonly_smoke_check(self, max_messages: int = 5) -> dict:
        service = self.service or self._build_service()
        profile = service.users().getProfile(
            userId=settings.gmail_user_id
        ).execute()
        response = service.users().messages().list(
            userId=settings.gmail_user_id,
            labelIds=["INBOX"],
            maxResults=max(0, min(max_messages, 5)),
        ).execute()
        return {
            "account": profile.get("emailAddress", "unknown"),
            "messages_found": len(response.get("messages", [])),
        }

    def _initial_message_ids(self, service, query=None) -> tuple[list[str], str | None]:
        ids = []
        page_token = None
        while True:
            request = service.users().messages().list(
                userId=settings.gmail_user_id,
                q=query or settings.gmail_initial_query,
                pageToken=page_token,
            )
            response = request.execute()
            ids.extend(item["id"] for item in response.get("messages", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return list(dict.fromkeys(ids)), None

    def _message_ids_from_history(
        self, service, cursor: str
    ) -> tuple[list[str], str | None]:
        ids = []
        page_token = None
        latest_history_id = cursor
        while True:
            response = service.users().history().list(
                userId=settings.gmail_user_id,
                startHistoryId=cursor,
                historyTypes=["messageAdded", "labelAdded"] if self.order_query else ["messageAdded"],
                pageToken=page_token,
            ).execute()
            latest_history_id = response.get("historyId", latest_history_id)
            for history in response.get("history", []):
                ids.extend(
                    entry["message"]["id"]
                    for entry in history.get("messagesAdded", [])
                )
                if self.order_query:
                    ids.extend(entry["message"]["id"] for entry in history.get("labelsAdded", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return list(dict.fromkeys(ids)), latest_history_id

    def _get_message(self, service, message_id: str) -> EmailMessage | None:
        try:
            raw = service.users().messages().get(
                userId=settings.gmail_user_id, id=message_id, format="full",
            ).execute()
        except Exception as error:
            if getattr(getattr(error, "resp", None), "status", None) == 404:
                return None
            raise
        if "INBOX" not in raw.get("labelIds", []):
            return None
        headers = {
            header["name"].casefold(): header["value"]
            for header in raw.get("payload", {}).get("headers", [])
            if isinstance(header.get("name"), str) and isinstance(header.get("value"), str)
        }
        sender_name, sender_email = parseaddr(headers.get("from", ""))
        try:
            received = datetime.fromtimestamp(int(raw.get("internalDate", "0")) / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            received = datetime.now(timezone.utc)
        return EmailMessage(
            external_message_id=raw["id"],
            sender_email=sender_email,
            sender_name=sender_name or None,
            subject=headers.get("subject"),
            body_text=self._body_text(raw.get("payload", {})),
            received_at=received,
        )

    @classmethod
    def _body_text(cls, payload: dict) -> str:
        plain_parts = cls._mime_parts(payload, "text/plain")
        if plain_parts:
            return "\n".join(plain_parts).strip()
        html_parts = cls._mime_parts(payload, "text/html")
        parser = _HTMLTextExtractor()
        parser.feed("\n".join(html_parts))
        return parser.text()

    @classmethod
    def _mime_parts(cls, payload: dict, mime_type: str, depth: int = 0) -> list[str]:
        if depth > 30:
            return []
        if payload.get("filename") or payload.get("body", {}).get("attachmentId"):
            return []
        parts = []
        if payload.get("mimeType") == mime_type:
            body = payload.get("body", {})
            data = body.get("data")
            if data and not body.get("attachmentId"):
                padding = "=" * (-len(data) % 4)
                try:
                    parts.append(base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")[:200000])
                except (ValueError, TypeError):
                    parts.append("")
        for child in payload.get("parts", []):
            parts.extend(cls._mime_parts(child, mime_type, depth + 1))
        return parts
