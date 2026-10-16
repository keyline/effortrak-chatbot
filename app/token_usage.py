"""Durable, metadata-only accounting; ambiguous requests retain their allowance."""

import asyncio
import json
import logging
import secrets
import sqlite3
from contextlib import suppress
from contextvars import ContextVar
from uuid import uuid4

from fastapi import HTTPException


conversation_context = ContextVar('usage_conversation', default=None)
log = logging.getLogger(__name__)


class TokenMeter:
    def __init__(self, path, backend):
        self.path = path
        self.backend = backend
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ai_usage_outbox (
                id TEXT PRIMARY KEY, reservation TEXT NOT NULL, settlement TEXT,
                delivered INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        self.worker = None
        self.flush_lock = asyncio.Lock()

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def start(self):
        self.worker = asyncio.create_task(self.run())

    async def close(self):
        if self.worker:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker
        with suppress(TimeoutError):
            await asyncio.wait_for(self.flush(), timeout=5)

    async def run(self):
        while True:
            await self.flush()
            await asyncio.sleep(30)

    async def flush(self, identifier=None):
        if self.flush_lock.locked():
            return
        async with self.flush_lock:
            with self.connect() as db:
                rows = db.execute('SELECT id, settlement FROM ai_usage_outbox WHERE delivered=0 AND settlement IS NOT NULL'
                                  + (' AND id=?' if identifier else '') + ' ORDER BY created_at LIMIT 20',
                                  (identifier,) if identifier else ()).fetchall()
            for identifier, payload in rows:
                try:
                    await self.backend.call('token-settle', '', json.loads(payload))
                except Exception as exc:
                    if isinstance(exc, HTTPException) and exc.status_code == 404 and json.loads(payload)['state'] == 'cancelled':
                        with self.connect() as db:
                            db.execute('UPDATE ai_usage_outbox SET delivered=1 WHERE id=?', (identifier,))
                        continue
                    # Do not log receipts, JWTs, provider bodies or employee metadata.
                    log.warning('AI usage delivery pending: %s', type(exc).__name__)
                    continue
                with self.connect() as db:
                    db.execute('UPDATE ai_usage_outbox SET delivered=1 WHERE id=?', (identifier,))

    def save(self, identifier, settlement):
        with self.connect() as db:
            db.execute('UPDATE ai_usage_outbox SET settlement=?, delivered=0 WHERE id=?', (json.dumps(settlement), identifier))

    async def create(self, client, identity, operation, **kwargs):
        # Use the provider's exact count for the same stateless input/tools/instructions.
        count_args = {key: value for key, value in kwargs.items() if key not in {'max_output_tokens', 'store'}}
        counted = await client.responses.input_tokens.count(**count_args)
        inputs = counted.input_tokens
        output_limit = kwargs.get('max_output_tokens')
        if type(inputs) is not int or inputs < 0 or type(output_limit) is not int or output_limit < 1:
            raise HTTPException(503, 'AI token budgeting is unavailable. Please retry later.')
        identifier, receipt = str(uuid4()), secrets.token_hex(32)
        reservation = {'id': identifier, 'receipt': receipt, 'tokens': inputs + output_limit,
                       'model': kwargs['model'], 'operation': operation,
                       'conversation_id': conversation_context.get()}
        with self.connect() as db:
            db.execute('INSERT INTO ai_usage_outbox(id,reservation) VALUES (?,?)', (identifier, json.dumps(reservation)))
        try:
            result = await self.backend.call('token-reserve', identity.credential, reservation)
            if result.get('id') != identifier:
                raise HTTPException(502, 'Invalid token reservation response.')
        except BaseException:
            # The request has not been submitted to OpenAI, so cancellation is safe.
            self.save(identifier, {'id': identifier, 'receipt': receipt, 'state': 'cancelled'})
            raise
        settlement = {'id': identifier, 'receipt': receipt, 'state': 'unknown'}
        # A NULL settlement is an in-flight/crashed call, never safe to auto-release.
        try:
            response = await client.with_options(max_retries=0).responses.create(**kwargs)
            usage = getattr(response, 'usage', None)
            response_id = getattr(response, 'id', None)
            if isinstance(response_id, str):
                settlement['response_id'] = response_id
            values = {'input_tokens': getattr(usage, 'input_tokens', None),
                      'output_tokens': getattr(usage, 'output_tokens', None),
                      'total_tokens': getattr(usage, 'total_tokens', None),
                      'cached_tokens': getattr(getattr(usage, 'input_tokens_details', None), 'cached_tokens', 0)}
            if (all(type(value) is int and value >= 0 for value in values.values())
                    and values['input_tokens'] + values['output_tokens'] == values['total_tokens']
                    and values['cached_tokens'] <= values['input_tokens'] and isinstance(response_id, str)):
                settlement.update(state='settled', **values)
            else:
                log.error('AI response did not include valid accounting usage; reservation retained')
            return response
        finally:
            self.save(identifier, settlement)
            # Persist before parsing the result or performing any subsequent business action.
            await self.flush(identifier)
