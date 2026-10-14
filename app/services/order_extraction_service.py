from typing import Protocol

from app.services.email_parser import EmailParser, ExtractedOrderLine


class OrderExtractor(Protocol):
    def extract(self, body_text: str) -> list[ExtractedOrderLine]: ...


class OrderExtractionService:
    def __init__(self, parser: EmailParser | None = None) -> None:
        self.parser = parser or EmailParser()

    def extract(self, body_text: str) -> list[ExtractedOrderLine]:
        return self.parser.parse_lines(body_text)

    def extract_message(self, message):
        """Structured classification from both subject and body."""
        return self.parser.parse_message(
            message.subject, message.body_text, sender_name=message.sender_name)
