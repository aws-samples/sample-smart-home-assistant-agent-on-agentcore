import React from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import Popover from '@cloudscape-design/components/popover';
import Icon from '@cloudscape-design/components/icon';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { DemoDataBadge } from './DemoDataBadge';

interface Props {
  title: string;
  /** Long-form explanation. Moved into a popover so it costs no wall height. */
  info?: string;
  /** Provenance key when this panel's numbers are simulated. */
  demoProvenanceKey?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
}

/**
 * The uniform frame every wall panel wears.
 *
 * On a monitoring wall the panel titles are scanning anchors, so they stay
 * short (h3, no wrapping paragraph). The prose that used to sit under each
 * heading — often 2-3 lines, which is what made the old stacked layout ~5000px
 * tall — moves behind an info icon. It is still one click away, and the caveats
 * it carries (derived rollout stage, TTFT provenance, the 28:1 token ratio) are
 * exactly the kind of thing an operator reads once and then wants out of the way.
 */
export function Panel({ title, info, demoProvenanceKey, actions, children }: Props) {
  return (
    <Container
      fitHeight
      header={
        <Header
          variant="h3"
          actions={actions}
          info={
            info ? (
              <Popover
                dismissButton={false}
                position="bottom"
                size="medium"
                triggerType="custom"
                header={title}
                content={<Box variant="p">{info}</Box>}
              >
                <span style={{ cursor: 'help' }}>
                  <Icon name="status-info" size="small" variant="subtle" />
                </span>
              </Popover>
            ) : undefined
          }
        >
          <SpaceBetween direction="horizontal" size="xs">
            <span>{title}</span>
            {demoProvenanceKey && <DemoDataBadge provenanceKey={demoProvenanceKey} />}
          </SpaceBetween>
        </Header>
      }
    >
      {children}
    </Container>
  );
}
