// Run with `npm test` (node --test; Node 22 strips the types itself).
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { retryOnSessionConflict, isSessionConflictResponse } from '../src/api/sessionConflict.ts';

const noSleep = async () => {};

function conflictResponse(): Response {
  return new Response('{"message":"Session operation in progress, please retry"}', {
    status: 409,
    headers: { 'x-amzn-errortype': 'RetryableConflictException:http://internal.amazon.com/coral/' },
  });
}

test('a fetch that hits the session-creation conflict is retried until it gets through', async () => {
  let calls = 0;
  const res = await retryOnSessionConflict(
    async () => (++calls < 3 ? conflictResponse() : new Response('ok', { status: 200 })),
    isSessionConflictResponse,
    noSleep,
  );
  assert.equal(res.status, 200);
  assert.equal(calls, 3);
});

test('a thrown RetryableConflictException from the SDK is retried until it gets through', async () => {
  let calls = 0;
  const out = await retryOnSessionConflict(async () => {
    if (++calls < 2) throw Object.assign(new Error('Session operation in progress'), { name: 'RetryableConflictException' });
    return 'listed';
  }, undefined, noSleep);
  assert.equal(out, 'listed');
  assert.equal(calls, 2);
});

test('the wait between attempts backs off and stops once the budget is spent', async () => {
  const waits: number[] = [];
  let calls = 0;
  const res = await retryOnSessionConflict(
    async () => { calls++; return conflictResponse(); },
    isSessionConflictResponse,
    async (ms) => { waits.push(ms); },
  );
  // The budget has to outlast a V2 session create, measured at up to ~12.6s.
  assert.deepEqual(waits, [500, 1000, 2000, 4000, 8000]);
  assert.equal(calls, 6);
  assert.equal(res.status, 409, 'the last conflict is handed back, not swallowed');
});

test('any other 409 is not retried', async () => {
  let calls = 0;
  const res = await retryOnSessionConflict(async () => {
    calls++;
    return new Response('{}', { status: 409, headers: { 'x-amzn-errortype': 'ConflictException' } });
  }, isSessionConflictResponse, noSleep);
  assert.equal(res.status, 409);
  assert.equal(calls, 1);
});

test('other failures are not retried', async () => {
  let calls = 0;
  const res = await retryOnSessionConflict(
    async () => { calls++; return new Response('{}', { status: 424 }); },
    isSessionConflictResponse,
    noSleep,
  );
  assert.equal(res.status, 424);
  assert.equal(calls, 1);

  let thrown = 0;
  await assert.rejects(retryOnSessionConflict(async () => {
    thrown++;
    throw Object.assign(new Error('denied'), { name: 'AccessDeniedException' });
  }, undefined, noSleep), /denied/);
  assert.equal(thrown, 1);
});
