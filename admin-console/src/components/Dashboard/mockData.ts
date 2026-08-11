/**
 * Simulated data for the dashboard cards that have NO real source in this
 * project. Every consumer of this file must render the "Demo data" badge so
 * an admin is never misled about which numbers are real.
 *
 * Only ONE card is left here.
 *
 *  - Budget consumption (#3): there is no billing/budget module in this
 *    project at all, and Cost Explorer only resolves to ACCOUNT level
 *    ($314.17 Bedrock over 30d) — it cannot be split per user/agent. Both the
 *    budget ceiling and the per-tenant split are therefore invented.
 *
 * Satisfaction (#6) used to live here and is now REAL (2026-08-11). The
 * chatbot grew a per-turn 👍/👎 control writing to `smarthome-feedback`, and
 * dashboard.py's `_fetch_satisfaction` aggregates it. The old note here said
 * "the chatbot has no thumbs up/down UI (grep found none)" — true when it was
 * written, and exactly the kind of investigated finding that goes stale
 * silently, so it is removed rather than left to mislead.
 *
 * Real data lives in dashboard.py; nothing here ever mixes into it.
 */

export interface MockBudget {
  scope: string;
  usedTokens: number;
  budgetTokens: number;
}

/** #3 — budget consumption per tenant. */
export const MOCK_BUDGETS: MockBudget[] = [
  { scope: 'tenant-default', usedTokens: 611098, budgetTokens: 1000000 },
  { scope: 'tenant-ab-targets', usedTokens: 187430, budgetTokens: 200000 },
  { scope: 'tenant-ab-bundles', usedTokens: 42980, budgetTokens: 250000 },
];

/** What it would take to make each mocked card real — shown in the info popover. */
export const MOCK_PROVENANCE = {
  budget:
    'requiresBillingModule',
} as const;
