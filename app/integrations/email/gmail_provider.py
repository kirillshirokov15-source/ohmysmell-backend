import asyncio
import base64
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


class GmailEmailProvider:
    def __init__(self, service=None) -> None:
        self.service = service

    async def fetch_unprocessed(
        self, cursor: str | None = None
    ) -> EmailFetchBatch:
        return await asyncio.to_thread(self._fetch_sync, cursor)

    def _fetch_sync(self, cursor: str | None) -> EmailFetchBatch:
        service = self.service or self._build_service()
        if cursor:
            try:
                message_ids, next_cursor = self._message_ids_from_history(
                    service, cursor
                )
            except Exception as error:
                if getattr(getattr(error, "resp", None), "status", None) != 404:
                    raise
                message_ids, next_cursor = self._bootstrap(service)
        else:
            message_ids, next_cursor = self._bootstrap(service)
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

    def _bootstrap(self, service) -> tuple[list[str], str | None]:
        # Capture the cursor before listing. Messages arriving during the list
        # are then visible through history on the next poll (duplicates remain
        # harmless because external_message_id is unique).
        profile = service.users().getProfile(
            userId=settings.gmail_user_id
        ).execute()
        message_ids, _ = self._initial_message_ids(service)
        return message_ids, profile.get("historyId")

    def _build_service(self):
        from googleapiclient.discovery import build

        credentials = load_gmail_credentials(allow_interactive=False)
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)

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

    def _initial_message_ids(self, service) -> tuple[list[str], str | None]:
        ids = []
        page_token = None
        while True:
            request = service.users().messages().list(
                userId=settings.gmail_user_id,
                q=settings.gmail_initial_query,
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
                historyTypes=["messageAdded"],
                pageToken=page_token,
            ).execute()
            latest_history_id = response.get("historyId", latest_history_id)
            for history in response.get("history", []):
                ids.extend(
                    entry["message"]["id"]
                    for entry in history.get("messagesAdded", [])
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return list(dict.fromkeys(ids)), latest_history_id

    def _get_message(self, service, message_id: str) -> EmailMessage | None:
        raw = service.users().messages().get(
            userId=settings.gmail_user_id,
            id=message_id,
            format="full",
        ).execute()
        if "INBOX" not in raw.get("labelIds", []):
            return None
        headers = {
            header["name"].casefold(): header["value"]
            for header in raw.get("payload", {}).get("headers", [])
        }
        sender_name, sender_email = parseaddr(headers.get("from", ""))
        return EmailMessage(
            external_message_id=raw["id"],
            sender_email=sender_email,
            sender_name=sender_name or None,
            subject=headers.get("subject"),
            body_text=self._body_text(raw.get("payload", {})),
            received_at=datetime.fromtimestamp(
                int(raw["internalDate"]) / 1000, tz=timezone.utc
            ),
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
    def _mime_parts(cls, payload: dict, mime_type: str) -> list[str]:
        parts = []
        if payload.get("mimeType") == mime_type:
            body = payload.get("body", {})
            data = body.get("data")
            if data and not body.get("attachmentId"):
                padding = "=" * (-len(data) % 4)
                parts.append(
                    base64.urlsafe_b64decode(data + padding).decode(
                        "utf-8", errors="replace"
                    )
                )
        for child in payload.get("parts", []):
            parts.extend(cls._mime_parts(child, mime_type))
        return parts
