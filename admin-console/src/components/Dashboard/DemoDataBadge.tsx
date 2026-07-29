import React from 'react';
import Badge from '@cloudscape-design/components/badge';
import Popover from '@cloudscape-design/components/popover';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { useI18n } from '../../i18n';

interface Props {
  /** i18n key describing what a real data source would require. */
  provenanceKey: string;
}

/**
 * Marks a card whose numbers are simulated, with a popover spelling out what
 * a real source would need. Without this an admin cannot tell the invented
 * cards (budget, satisfaction) from the measured ones.
 */
export function DemoDataBadge({ provenanceKey }: Props) {
  const { t } = useI18n();
  return (
    <Popover
      dismissButton={false}
      position="top"
      size="medium"
      triggerType="custom"
      header={t('dashboard.demoDataTitle')}
      content={
        <SpaceBetween size="xs">
          <Box variant="p">{t('dashboard.demoDataBody')}</Box>
          <Box variant="p" color="text-body-secondary">
            {t(`dashboard.provenance.${provenanceKey}`)}
          </Box>
        </SpaceBetween>
      }
    >
      <span style={{ cursor: 'help' }}>
        <Badge color="grey">{t('dashboard.demoData')}</Badge>
      </span>
    </Popover>
  );
}
