import React from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { MOCK_BUDGETS, MOCK_PROVENANCE } from './mockData';
import { utilisationSeverity } from './palette';
import { DemoDataBadge } from './DemoDataBadge';
import { compact } from './KpiRow';
import { useI18n } from '../../i18n';

/**
 * #3 Budget consumption — SIMULATED.
 *
 * A meter per scope, because a single ratio against a limit is a meter, not a
 * two-slice pie. Cloudscape's ProgressBar carries its own status semantics, so
 * near-threshold rows escalate to `status="error"` and pair it with an icon and
 * text label rather than leaning on colour.
 *
 * No real source exists: this project has no billing module and Cost Explorer
 * only resolves to account level, so a per-tenant token budget cannot be
 * derived. See mockData.ts.
 */
export function BudgetMeter() {
  const { t } = useI18n();

  return (
    <Container
      header={
        <Header
          variant="h3"
          description={t('dashboard.budget.desc')}
          actions={<DemoDataBadge provenanceKey={MOCK_PROVENANCE.budget} />}
        >
          {t('dashboard.budget.title')}
        </Header>
      }
    >
      <SpaceBetween size="l">
        {MOCK_BUDGETS.map((b) => {
          const ratio = b.budgetTokens ? b.usedTokens / b.budgetTokens : 0;
          const severity = utilisationSeverity(ratio);
          const nearLimit = severity === 'critical' || severity === 'serious';
          return (
            <SpaceBetween size="xxs" key={b.scope}>
              <ProgressBar
                value={Math.min(100, ratio * 100)}
                status={severity === 'critical' ? 'error' : 'in-progress'}
                label={b.scope}
                description={t('dashboard.budget.usage')
                  .replace('{used}', compact(b.usedTokens, 1))
                  .replace('{total}', compact(b.budgetTokens, 1))}
                additionalInfo={`${(ratio * 100).toFixed(1)}%`}
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
    </Container>
  );
}
