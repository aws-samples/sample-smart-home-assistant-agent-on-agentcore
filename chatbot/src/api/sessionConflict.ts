/**
 * Retry a runtime call that lost the race to create its own session.
 *
 * Every login opens a brand-new runtime session id, and two calls fire on it
 * at once: the `__warmup__` POST and the side panel's Files listing
 * (InvokeAgentRuntimeCommand). Whichever arrives second while the first is
 * still creating the session is refused with
 * `409 RetryableConflictException: Session operation in progress, please retry`.
 * On platform V2 that happens every time (docs/architecture-and-design.md
 * §9.2.1). The refusal comes before the container is entered, so nothing ran
 * and the call is safe to repeat.
 *
 * The SDK marks the error retryable, but its default budget (3 attempts,
 * under a second) is spent long before a session create finishes, which was
 * measured at up to ~12.6s. Hence a budget of its own.
 */

// Waits between attempts: 6 attempts over ~15.5s.
const RETRY_DELAYS_MS = [500, 1000, 2000, 4000, 8000];

const CONFLICT = 'RetryableConflictException';

/** True for the one 409 that means "this session is still being created". */
export function isSessionConflictResponse(res: Response): boolean {
  return res.status === 409 && (res.headers.get('x-amzn-errortype') ?? '').startsWith(CONFLICT);
}

/**
 * Run `call`, repeating it while it hits the session-creation conflict: a
 * thrown error named RetryableConflictException (SDK clients), or a result
 * `isConflict` recognises (raw fetch). Any other outcome is returned or
 * thrown as is. Once the budget is spent the last conflict is handed back.
 */
export async function retryOnSessionConflict<T>(
  call: () => Promise<T>,
  isConflict: (result: T) => boolean = () => false,
  sleep: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
): Promise<T> {
  for (const delay of RETRY_DELAYS_MS) {
    try {
      const result = await call();
      if (!isConflict(result)) return result;
    } catch (e) {
      if ((e as { name?: string })?.name !== CONFLICT) throw e;
    }
    await sleep(delay);
  }
  return call();
}
