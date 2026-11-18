"""Exercise the real supplier modules with fake Gmail and database boundaries."""
import asyncio
import base64
from contextlib import contextmanager
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
LIMIT = 32


def module(name, **values):
    result = ModuleType(name)
    result.__dict__.update(values)
    return result


@contextmanager
def source(path, dependencies):
    # Only imported integration boundaries are replaced, never the code under test.
    target = ModuleType('supplier_resilience_subject')
    with patch.dict(sys.modules, dependencies):
        exec(compile((ROOT / path).read_text(), str(ROOT / path), 'exec'), target.__dict__)
        yield target


def response(value):
    return SimpleNamespace(execute=lambda: value)


def part(identifier, size):
    return {'filename': identifier + '.xlsx', 'body': {'attachmentId': identifier, 'size': size}}


def provider_dependencies():
    return {
        'app.integrations.email.gmail_provider': module('gmail_provider',
            GmailEmailProvider=SimpleNamespace(_body_text=lambda payload: ''), ReadOnlyHttp=object),
        'app.integrations.email.gmail_auth': module('gmail_auth', load_gmail_credentials=Mock()),
        'app.integrations.email.mailboxes': module('mailboxes',
            mailbox=lambda role: SimpleNamespace(user_id='me'),
            expected_email=lambda role: 'supplier@example.invalid',
            verify_identity=lambda role, profile: profile['emailAddress']),
        'app.services.buying_excel': module('buying_excel', MAX_UPLOAD=LIMIT),
    }


def gmail(parts, encoded, *, authentication=True):
    service, api = Mock(), Mock()
    service.users.return_value.getProfile.return_value = response({'emailAddress': 'supplier@example.invalid'})
    service.users.return_value.messages.return_value = api
    api.list.return_value = response({'messages': [{'id': 'message'}], 'nextPageToken': 'next-page'})
    headers = [{'name': 'From', 'value': 'prices@supplier.invalid'}]
    if authentication:
        headers.append({'name': 'Authentication-Results',
                        'value': 'mx.google.com; dmarc=pass header.from=supplier.invalid'})
    api.get.return_value = response({'payload': {'headers': headers, 'parts': parts}})
    api.attachments.return_value.get.side_effect = lambda **kwargs: response({'data': encoded[kwargs['id']]})
    return service, api


class SupplierAttachmentTests(unittest.TestCase):
    def test_oversized_attachment_does_not_hide_valid_sibling_or_next_page(self):
        service, api = gmail([part('large', LIMIT + 1), part('valid', 5)],
                             {'valid': base64.urlsafe_b64encode(b'price').decode()})
        with source('app/integrations/email/supplier_gmail.py', provider_dependencies()) as code:
            provider = code.SupplierGmailProvider(service)
            attachments, cursor = provider.price_attachments('previous-page')
        self.assertEqual([row['data'] for row in attachments], [b'price'])
        self.assertEqual(cursor, 'next-page')
        self.assertEqual(provider.price_attachments_skipped, 1)
        self.assertEqual([call.kwargs['id'] for call in api.attachments.return_value.get.call_args_list], ['valid'])
        self.assertEqual(api.list.call_args.kwargs['pageToken'], 'previous-page')
        api.send.assert_not_called()
        api.modify.assert_not_called()

    def test_empty_malformed_and_oversized_responses_are_skipped(self):
        parts = [part('zero', 0), part('encoded-large', 1), part('decoded-large', 1),
                 part('malformed', 1), part('empty', 1), part('valid', LIMIT)]
        encoded = {'encoded-large': 'A' * (LIMIT * 2 + 1),
                   'decoded-large': base64.urlsafe_b64encode(b'x' * (LIMIT + 1)).decode(),
                   'malformed': '!!!!', 'empty': '',
                   'valid': base64.urlsafe_b64encode(b'x' * LIMIT).decode()}
        service, api = gmail(parts, encoded)
        with source('app/integrations/email/supplier_gmail.py', provider_dependencies()) as code:
            provider = code.SupplierGmailProvider(service)
            attachments, _ = provider.price_attachments()
            self.assertEqual(provider.price_attachments_skipped, 5)
            self.assertEqual(attachments[0]['data'], b'x' * LIMIT)
            self.assertEqual(len(attachments), 1)
            # Counts belong to one poll and are reset for the next sweep.
            provider.price_attachments()
            self.assertEqual(provider.price_attachments_skipped, 5)
        self.assertNotIn('zero', [call.kwargs['id'] for call in api.attachments.return_value.get.call_args_list])

    def test_unverified_sender_is_never_downloaded(self):
        service, api = gmail([part('valid', 5)], {}, authentication=False)
        with source('app/integrations/email/supplier_gmail.py', provider_dependencies()) as code:
            attachments, _ = code.SupplierGmailProvider(service).price_attachments()
        self.assertEqual(attachments, [])
        api.attachments.return_value.get.assert_not_called()

    def test_gmail_failure_still_propagates(self):
        service, api = gmail([part('valid', 5)], {})
        api.attachments.return_value.get.side_effect = ConnectionError('synthetic outage')
        with source('app/integrations/email/supplier_gmail.py', provider_dependencies()) as code:
            with self.assertRaises(ConnectionError):
                code.SupplierGmailProvider(service).price_attachments()


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def begin(self):
        return self


def worker_dependencies(importer):
    return {
        'app.integrations.email.supplier_gmail': module('supplier_gmail', SupplierGmailProvider=object),
        'app.workers.supplier_replies': module('supplier_replies', SupplierReplyPoller=object),
        'app.services.supplier_price_import': module('supplier_price_import', import_attachment=importer),
        'app.database.session': module('database_session', async_session=Transaction),
        'app.integrations.moysklad.async_gateway': module('async_gateway', AsyncMoySkladGateway=object),
        'app.integrations.moysklad.client': module('moysklad_client', MoySkladClient=object),
    }


class SupplierWorkerTests(unittest.TestCase):
    def test_rejected_price_does_not_block_replies_or_valid_price(self):
        importer = AsyncMock(side_effect=[ValueError('synthetic invalid layout'), None])
        provider = SimpleNamespace(readonly_smoke_check=Mock(), price_attachments_skipped=1,
                                   price_attachments=Mock(return_value=([{'filename': 'bad.xlsx'},
                                                                         {'filename': 'good.xlsx'}], 'next')))
        poller = SimpleNamespace(run_once=AsyncMock(return_value=3))
        with source('app/workers/supplier_email_runtime.py', worker_dependencies(importer)) as code:
            worker = code.SupplierEmailWorker(provider, poller=poller, sessions=Transaction,
                                               catalog=AsyncMock(return_value=[]))
            stats = asyncio.run(worker.run_once())
        self.assertEqual(stats, {'received': 6, 'processed': 4, 'failed': 0, 'skipped': 2})
        self.assertEqual(worker.price_page, 'next')
        self.assertEqual(importer.await_count, 2)
        poller.run_once.assert_awaited_once()

    def test_database_failure_is_not_silently_skipped(self):
        importer = AsyncMock(side_effect=ConnectionError('synthetic database outage'))
        provider = SimpleNamespace(readonly_smoke_check=Mock(),
                                   price_attachments=Mock(return_value=([{'filename': 'valid.xlsx'}], 'next')))
        with source('app/workers/supplier_email_runtime.py', worker_dependencies(importer)) as code:
            worker = code.SupplierEmailWorker(provider, poller=SimpleNamespace(run_once=AsyncMock(return_value=0)),
                                               sessions=Transaction, catalog=AsyncMock(return_value=[]))
            with self.assertRaises(ConnectionError):
                asyncio.run(worker.run_once())
        self.assertIsNone(worker.price_page)


if __name__ == '__main__':
    unittest.main()
