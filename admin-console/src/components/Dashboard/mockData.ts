/**
 * Simulated data for the dashboard cards that have NO real source in this
 * project. Every consumer of this file must render the "Demo data" badge so
 * an admin is never misled about which numbers are real.
 *
 * Investigated 2026-07-29 (see the spec's §2 for the raw evidence):
 *
 *  - Budget consumption (#3): there is no billing/budget module in this
 *    project at all, and Cost Explorer only resolves to ACCOUNT level
 *    ($314.17 Bedrock over 30d) — it cannot be split per user/agent. Both the
 *    budget ceiling and the per-tenant split are therefore invented.
 *
 *  - Satisfaction (#6): the chatbot has no thumbs up/down UI (grep found
 *    none). The only feedback path is the `user-feedback` skill writing JSON
 *    files to the runtime's /mnt/workspace/feedback/, readable solely through
 *    Remote Shell — not aggregatable. "AI-inferred CSAT at 100% coverage"
 *    does not exist. Escalation rate has no instrumentation either.
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

/** #6 — satisfaction headline figures. */
export const MOCK_SATISFACTION = {
  csat: 4.3,
  csatScale: 5,
  thumbsUp: 128,
  thumbsDown: 19,
  /** Escalation rate per day, oldest first. */
  escalationTrend: [
    { day: '2026-07-23', rate: 0.084 },
    { day: '2026-07-24', rate: 0.077 },
    { day: '2026-07-25', rate: 0.091 },
    { day: '2026-07-26', rate: 0.068 },
    { day: '2026-07-27', rate: 0.059 },
    { day: '2026-07-28', rate: 0.062 },
    { day: '2026-07-29', rate: 0.054 },
  ],
};

/** What it would take to make each mocked card real — shown in the info popover. */
export const MOCK_PROVENANCE = {
  budget:
    'requiresBillingModule',
  satisfaction:
    'requiresChatbotFeedback',
} as const;
