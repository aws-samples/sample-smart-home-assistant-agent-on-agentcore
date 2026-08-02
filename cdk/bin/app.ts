#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { SmartHomeStack } from "../lib/smarthome-stack";

const app = new cdk.App();

// Region resolution must match scripts/0[1-7]-*.sh and scripts/*.py, which all
// use AWS_DEFAULT_REGION -> AWS_REGION -> us-west-2. CDK_DEFAULT_REGION is
// deliberately NOT consulted: the CDK CLI derives it from the full credential
// chain, which includes EC2 instance metadata. On an EC2 host outside us-west-2
// that silently sent the CDK stack to the instance's own region while the
// AgentCore/Cognito scripts still targeted us-west-2, splitting one deployment
// across two regions. Set AWS_DEFAULT_REGION to deploy elsewhere.
const region =
  process.env.AWS_DEFAULT_REGION || process.env.AWS_REGION || "us-west-2";

new SmartHomeStack(app, "SmartHomeAssistantStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region,
  },
  description: "Smart Home Assistant - IoT Device Simulator with AI Agent",
});
