import React from 'react';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { MOCK_BUDGETS } from './mockData';
import { utilisationSeverity } from './palette';
import { compact } from './format';
import { useI18n } from '../../i18n';

/**
 * #3 Budget consumption — SIMULATED. Panel body.
 *
 * A meter per scope: a single ratio against a limit is a meter, not a two-slice
 * pie. Near-threshold rows escalate to ProgressBar's error status AND carry an
 * icon + text label, so severity never rests on colour alone.
 *
 * No real source: this project has no billing module, and Cost Explorer only
 * resolves to account level so a per-tenant token budget cannot be derived.
 * See mockData.ts.
 */
export function BudgetMeter() {
  const { t } = useI18n();

  return (
    <SpaceBetween size="m">
      {MOCK_BUDGETS.map((b) => {
        const ratio = b.budgetTokens ? b.usedTokens / b.budgetTokens : 0;
        const severity = utilisationSeverity(ratio);
        const nearLimit = severity === 'critical' || severity === 'serious';
        return (
          <SpaceBetween size="xxxs" key={b.scope}>
            <ProgressBar
              value={Math.min(100, ratio * 100)}
              status={severity === 'critical' ? 'error' : 'in-progress'}
              label={b.scope}
              description={t('dashboard.budget.usage')
                .replace('{used}', compact(b.usedTokens, 1))
                .replace('{total}', compact(b.budgetTokens, 1))}
            />
            {nearLimit && (
              <StatusIndicator type={severity === 'critical' ? 'error' : 'warning'}>
                {t('dashboard.budget.nearThreshold')}
              </StatusIndicator>
            )}
          </SpaceBetween>
        );
      })}
      <Box variant="small" color="text-body-secondary">
        {t('dashboard.budget.footnote')}
      </Box>
    </SpaceBetween>
  );
}
