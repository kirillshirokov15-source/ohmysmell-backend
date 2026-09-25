from app.integrations.email.gmail_provider import GmailEmailProvider


def main() -> None:
    result = GmailEmailProvider().readonly_smoke_check(max_messages=5)
    print("Gmail OAuth: OK")
    print(f"Account: {result['account']}")
    print("INBOX access: OK")
    print(f"Messages found: {result['messages_found']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        from app.integrations.email.health import classify
        print('Gmail OAuth: ' + classify(error))
        print('Recovery: run app.scripts.gmail_oauth explicitly, then repeat this smoke test')
        raise SystemExit(1)
