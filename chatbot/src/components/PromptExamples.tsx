import React, { useMemo, useState } from 'react';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Header from '@cloudscape-design/components/header';
import Input from '@cloudscape-design/components/input';
import SpaceBetween from '@cloudscape-design/components/space-between';
import library from '../generated/prompt-examples.json';
import { useI18n } from '../i18n';

/**
 * The example-prompt library.
 *
 * Reads shared/prompt-examples.json, copied here at build time by
 * scripts/01-install-deps.sh. scripts/sim/personas.py reads the SAME file to
 * generate demo traffic, which is the point: the chips and the simulated
 * conversations are one list of "what this system can do". Kept apart, they
 * drift, and the drift does not fail — you find out mid-demo that nothing
 * exercises the security agent.
 *
 * Two behaviours are deliberate:
 *
 *  - It stages the prompt in the input box instead of sending it. A presenter
 *    needs a beat to say what the example is about to demonstrate, and someone
 *    exploring wants to edit before committing.
 *  - It is reachable at any time, not only from the welcome screen. The old
 *    chips lived behind `messages.length === 0`, so the catalogue of features
 *    vanished permanently the moment anyone said hello.
 */

interface Example {
  zh: string;
  en: string;
  tier: string;
  noteZh?: string;
  noteEn?: string;
}

interface Group {
  id: string;
  titleZh: string;
  titleEn: string;
  covers: string[];
  examples: Example[];
}

interface Props {
  onPick: (prompt: string) => void;
  onClose: () => void;
}

export function PromptExamples({ onPick, onClose }: Props) {
  const { t, language } = useI18n();
  const [filter, setFilter] = useState('');

  const zh = language === 'zh';
  const groups = (library as { groups: Group[] }).groups;

  const text = (ex: Example) => (zh ? ex.zh : ex.en);
  const note = (ex: Example) => (zh ? ex.noteZh : ex.noteEn);
  const title = (g: Group) => (zh ? g.titleZh : g.titleEn);

  // Filter across BOTH languages plus the group title. A demo is often narrated
  // in one language while the prompts are read in the other, and someone
  // searching "security" should find 安全评估.
  const shown = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return groups;
    return groups
      .map((g) => ({
        ...g,
        examples: g.examples.filter(
          (ex) =>
            ex.zh.toLowerCase().includes(q) ||
            ex.en.toLowerCase().includes(q) ||
            g.titleZh.toLowerCase().includes(q) ||
            g.titleEn.toLowerCase().includes(q),
        ),
      }))
      .filter((g) => g.examples.length > 0);
  }, [filter, groups]);

  const total = groups.reduce((n, g) => n + g.examples.length, 0);

  return (
    <div className="examples-panel">
      <Box padding="s">
        <SpaceBetween size="s">
          <Header
            variant="h3"
            actions={<Button iconName="close" variant="icon" ariaLabel={t('examples.close')} onClick={onClose} />}
            description={t('examples.subtitle').replace('{count}', String(total))}
          >
            {t('examples.title')}
          </Header>

          <Input
            type="search"
            value={filter}
            onChange={({ detail }) => setFilter(detail.value)}
            placeholder={t('examples.search')}
            ariaLabel={t('examples.search')}
          />

          {shown.length === 0 && (
            <Box color="text-body-secondary" textAlign="center" padding="m">
              {t('examples.noMatch')}
            </Box>
          )}

          {shown.map((g) => (
            <ExpandableSection
              key={g.id}
              // Expanded while filtering: a collapsed section that contains the
              // only match reads as "no results".
              defaultExpanded={Boolean(filter.trim())}
              headerText={`${title(g)} (${g.examples.length})`}
            >
              <SpaceBetween size="xs">
                {g.examples.map((ex) => (
                  <div key={ex.en} className="example-row">
                    <Button variant="link" onClick={() => onPick(text(ex))}>
                      {text(ex)}
                    </Button>
                    <div className="example-meta">
                      {ex.tier === 'heavy' && <Badge color="blue">{t('examples.heavy')}</Badge>}
                      {note(ex) && (
                        <Box variant="small" color="text-body-secondary" display="inline">
                          {note(ex)}
                        </Box>
                      )}
                    </div>
                  </div>
                ))}
              </SpaceBetween>
            </ExpandableSection>
          ))}
        </SpaceBetween>
      </Box>
    </div>
  );
}

/** Welcome-screen chips: the first example of each group, from the same source. */
export function welcomeChips(zh: boolean, max = 8) {
  const groups = (library as { groups: Group[] }).groups;
  return groups.slice(0, max).map((g) => ({
    group: zh ? g.titleZh : g.titleEn,
    label: zh ? g.examples[0].zh : g.examples[0].en,
  }));
}

export default PromptExamples;
