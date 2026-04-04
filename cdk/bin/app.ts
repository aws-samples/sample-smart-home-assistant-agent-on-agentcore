#!/usr/bin/env node
import "source-map-support/register";
import * as cdk from "aws-cdk-lib";
import { SmartHomeStack } from "../lib/smarthome-stack";

const app = new cdk.App();

new SmartHomeStack(app, "SmartHomeAssistantStack", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION || "us-east-1",
  },
  description: "Smart Home Assistant - IoT Device Simulator with AI Agent",
});
