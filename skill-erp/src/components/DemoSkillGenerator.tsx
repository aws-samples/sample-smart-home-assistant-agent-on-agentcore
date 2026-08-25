import React, { useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Table from '@cloudscape-design/components/table';
import { createMyRecord } from '../api/erpApi';
import { useI18n } from '../i18n';
import templateData from '../generated/demo-skill-templates.json';

/**
 * Max width for a table column holding prose, paired with `<WrapCell>` in that column.
 *
 * An unconstrained column sizes itself to its content, so one long description stretches it
 * across the viewport and squeezes the rest of the row into the corner. Two things fix it:
 * the cell must wrap (Cloudscape truncates to one line otherwise), and the bound must sit
 * on the span rather than the column — CSS `max-width` on a `<td>` is only a hint to the
 * auto table-layout algorithm, and a cell with `max-width: 420px` was measured rendering
 * 481px wide. Per cell, and NOT via the Table's `wrapLines` prop: that applies to every
 * column and stacks the short ones letter by letter ("PE / NDING / _APPR / OVAL").
 */
const TEXT_COL_MAX = 420;

/** A prose table cell: bounded, and wrapping inside that bound instead of truncating. */
const WrapCell: React.FC<{ children?: React.ReactNode; max?: number }> = ({
  children,
  max = TEXT_COL_MAX,
}) => (
  <span style={{
    display: 'block',
    maxWidth: max,
    whiteSpace: 'normal',
    overflowWrap: 'break-word',
  }}>
    {children}
  </span>
);

/**
 * Ten demo Skills, generated on click and publishable to the Registry in one pass.
 *
 * The point of this panel is the scan demo downstream of it. A reviewer looking at an
 * empty approval queue has nothing to scan, and hand-writing ten skills with a spread of
 * planted risks takes longer than the demo does.
 *
 * The templates are DETERMINISTIC (shared/demo-skill-templates.json), not model-generated,
 * for two reasons that matter more than the novelty of generating them:
 *
 *   - Every run produces the same ten verdicts, so the demo is repeatable and
 *     cdk/lambda/admin-api/tests/test_skill_scan.py can assert them.
 *   - The risks are planted on purpose and spread across the rule families, so the
 *     report reads green/amber/red rather than uniformly one colour. A generator asked
 *     to invent ten skills would most likely produce ten harmless ones.
 *
 * Publishing goes through the ordinary `POST /my-skills`, one record at a time. Nothing
 * about these records is special server-side: they submit for approval like any other,
 * which is the whole point — the reviewer sees them in the queue they already use.
 */

interface BilingualLabel {
  en: string;
  zh: string;
}

interface DemoTemplate {
  id: string;
  type: BilingualLabel;
  riskProfile: BilingualLabel;
  skillName: string;
  description: string;
  allowedTools?: string[];
  license?: string;
  compatibility?: string;
  instructions: string;
  expected: {
    static: { verdict: string; riskTier: string; rules: string[] };
    semanticOnly?: boolean;
  };
}

const TEMPLATES: DemoTemplate[] = (templateData as any).templates;

type Outcome = { id: string; ok: boolean; message: string };

interface Props {
  onRegistered: () => void;
}

const DemoSkillGenerator: React.FC<Props> = ({ onRegistered }) => {
  const { t, language } = useI18n();
  const [generated, setGenerated] = useState<DemoTemplate[]>([]);
  const [registering, setRegistering] = useState(false);
  const [done, setDone] = useState(0);
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const [error, setError] = useState('');

  const label = (l: BilingualLabel) => (language === 'zh' ? l.zh : l.en);

  const handleGenerate = () => {
    setError('');
    setOutcomes([]);
    setDone(0);
    setGenerated(TEMPLATES);
  };

  const handleRegister = async () => {
    setError('');
    setOutcomes([]);
    setDone(0);
    setRegistering(true);
    const results: Outcome[] = [];
    // Sequential on purpose. Each create is a Registry write followed by a
    // submit-for-approval that has to wait for the record to leave CREATING, and ten of
    // those in parallel is how you find the Registry's throttling limits during a demo.
    for (const tpl of generated) {
      try {
        await createMyRecord({
          skillName: tpl.skillName,
          description: tpl.description,
          instructions: tpl.instructions,
          allowedTools: tpl.allowedTools || [],
          license: tpl.license,
          compatibility: tpl.compatibility,
        });
        results.push({ id: tpl.id, ok: true, message: t('erp.demo.registered') });
      } catch (err: any) {
        results.push({ id: tpl.id, ok: false, message: err.message });
      }
      setDone((n) => n + 1);
      setOutcomes([...results]);
    }
    setRegistering(false);
    onRegistered();
  };

  const outcomeFor = (id: string) => outcomes.find((o) => o.id === id);
  const failures = outcomes.filter((o) => !o.ok).length;

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={t('erp.demo.description')}
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={handleGenerate} disabled={registering}>
                {t('erp.demo.generate')}
              </Button>
              <Button
                variant="primary"
                onClick={handleRegister}
                loading={registering}
                disabled={generated.length === 0}
              >
                {t('erp.demo.register')}
              </Button>
            </SpaceBetween>
          }
        >
          {t('erp.demo.title')}
        </Header>
      }
    >
      <SpaceBetween size="m">
        {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}

        {/* Said before anything is generated, not after. These records carry planted
            credentials, shell pipelines and hidden instructions; someone who imported
            them into a real deployment because the panel looked like a starter kit would
            have a genuine problem. */}
        <Alert type="warning" header={t('erp.demo.warningTitle')}>
          {t('erp.demo.warningBody')}
        </Alert>

        {generated.length === 0 ? (
          <Box textAlign="center" padding="m" color="text-body-secondary">
            {t('erp.demo.empty')}
          </Box>
        ) : (
          <>
            {(registering || outcomes.length > 0) && (
              <ProgressBar
                value={(done / generated.length) * 100}
                additionalInfo={
                  failures > 0
                    ? t('erp.demo.progressWithFailures')
                        .replace('{done}', String(done))
                        .replace('{total}', String(generated.length))
                        .replace('{failed}', String(failures))
                    : t('erp.demo.progress')
                        .replace('{done}', String(done))
                        .replace('{total}', String(generated.length))
                }
                label={t('erp.demo.registering')}
              />
            )}
            <Table
              variant="embedded"
              contentDensity="compact"
              items={generated}
              trackBy="id"
              columnDefinitions={[
                {
                  id: 'name',
                  header: t('erp.demo.colName'),
                  cell: (r) => <code>{r.skillName}</code>,
                },
                { id: 'type', header: t('erp.demo.colType'), cell: (r) => label(r.type) },
                {
                  id: 'risk',
                  header: t('erp.demo.colRisk'),
                  minWidth: 260,
                  maxWidth: TEXT_COL_MAX,
                  cell: (r) => (
                    <SpaceBetween direction="horizontal" size="xxs">
                      <Badge
                        color={
                          r.expected.static.verdict === 'FAIL' || r.expected.semanticOnly
                            ? 'red'
                            : r.expected.static.verdict === 'WARN'
                              ? 'blue'
                              : 'green'
                        }
                      >
                        {r.expected.semanticOnly
                          ? t('erp.demo.semanticOnly')
                          : r.expected.static.verdict}
                      </Badge>
                      <Box variant="small">
                        <WrapCell>{label(r.riskProfile)}</WrapCell>
                      </Box>
                    </SpaceBetween>
                  ),
                },
                {
                  id: 'status',
                  header: t('erp.demo.colStatus'),
                  cell: (r) => {
                    const o = outcomeFor(r.id);
                    if (!o) return <Box variant="small" color="text-body-secondary">—</Box>;
                    return o.ok
                      ? <StatusIndicator type="success">{o.message}</StatusIndicator>
                      : <StatusIndicator type="error">{o.message}</StatusIndicator>;
                  },
                },
              ]}
            />
            <Box variant="small" color="text-body-secondary">
              {t('erp.demo.rerunHint')}
            </Box>
          </>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default DemoSkillGenerator;
