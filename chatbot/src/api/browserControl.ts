/**
 * Take/Release control of an AgentCore live browser session.
 *
 * Per the AgentCore samples (interactive_tools/browser_viewer.py), control
 * switching is just UpdateBrowserStream with streamStatus = "DISABLED" (to
 * take) or "ENABLED" (to release). When DISABLED the automation CDP stream
 * is cut, so any running browser-use loop loses its WebSocket and the
 * browser becomes free for the human to drive via the DCV live-view stream.
 *
 * The chatbot calls UpdateBrowserStream directly with Identity-Pool creds
 * (same pattern as workspace file probing) — no Lambda hop needed.
 */
import { getAwsCredentials } from '../auth/CognitoAuth';
import { getConfig } from '../config';

const DEFAULT_BROWSER_IDENTIFIER = 'aws.browser.v1';

async function callUpdate(
  browserIdentifier: string,
  sessionId: string,
  streamStatus: 'ENABLED' | 'DISABLED',
): Promise<void> {
  const sdk = await import('@aws-sdk/client-bedrock-agentcore');
  const credentials = await getAwsCredentials();
  const client = new sdk.BedrockAgentCoreClient({
    region: getConfig().region,
    credentials,
  });
  const cmd = new sdk.UpdateBrowserStreamCommand({
    browserIdentifier,
    sessionId,
    streamUpdate: {
      automationStreamUpdate: { streamStatus },
    },
  });
  await client.send(cmd);
}

export async function takeControl(sessionId: string, browserIdentifier?: string): Promise<void> {
  await callUpdate(browserIdentifier || DEFAULT_BROWSER_IDENTIFIER, sessionId, 'DISABLED');
}

export async function releaseControl(sessionId: string, browserIdentifier?: string): Promise<void> {
  await callUpdate(browserIdentifier || DEFAULT_BROWSER_IDENTIFIER, sessionId, 'ENABLED');
}
