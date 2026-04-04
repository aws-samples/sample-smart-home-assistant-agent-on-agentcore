import * as cdk from "aws-cdk-lib";
import * as cognito from "aws-cdk-lib/aws-cognito";
import * as iot from "aws-cdk-lib/aws-iot";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as cr from "aws-cdk-lib/custom-resources";
import * as logs from "aws-cdk-lib/aws-logs";
import { Construct } from "constructs";
import * as path from "path";

export class SmartHomeStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ========================
    // Cognito User Pool
    // ========================
    const userPool = new cognito.UserPool(this, "SmartHomeUserPool", {
      userPoolName: "smarthome-assistant-users",
      selfSignUpEnabled: true,
      signInAliases: { email: true },
      autoVerify: { email: true },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const userPoolClient = userPool.addClient("SmartHomeAppClient", {
      userPoolClientName: "smarthome-app-client",
      authFlows: { userPassword: true, userSrp: true },
      oAuth: {
        flows: { authorizationCodeGrant: true, implicitCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.PROFILE],
        callbackUrls: ["http://localhost:3000/callback", "https://localhost/callback"],
        logoutUrls: ["http://localhost:3000/", "https://localhost/"],
      },
    });

    const userPoolDomain = userPool.addDomain("SmartHomeDomain", {
      cognitoDomain: { domainPrefix: `smarthome-${cdk.Aws.ACCOUNT_ID}` },
    });

    // ========================
    // Cognito Identity Pool (for IoT device simulator)
    // ========================
    const identityPool = new cognito.CfnIdentityPool(this, "SmartHomeIdentityPool", {
      identityPoolName: "smarthome_identity_pool",
      allowUnauthenticatedIdentities: true,
      cognitoIdentityProviders: [{
        clientId: userPoolClient.userPoolClientId,
        providerName: userPool.userPoolProviderName,
      }],
    });

    const unauthRole = new iam.Role(this, "CognitoUnauthRole", {
      assumedBy: new iam.FederatedPrincipal("cognito-identity.amazonaws.com", {
        StringEquals: { "cognito-identity.amazonaws.com:aud": identityPool.ref },
        "ForAnyValue:StringLike": { "cognito-identity.amazonaws.com:amr": "unauthenticated" },
      }, "sts:AssumeRoleWithWebIdentity"),
    });
    unauthRole.addToPolicy(new iam.PolicyStatement({
      actions: ["iot:Connect", "iot:Subscribe", "iot:Receive", "iot:Publish"],
      resources: ["*"],
    }));

    const authRole = new iam.Role(this, "CognitoAuthRole", {
      assumedBy: new iam.FederatedPrincipal("cognito-identity.amazonaws.com", {
        StringEquals: { "cognito-identity.amazonaws.com:aud": identityPool.ref },
        "ForAnyValue:StringLike": { "cognito-identity.amazonaws.com:amr": "authenticated" },
      }, "sts:AssumeRoleWithWebIdentity"),
    });
    authRole.addToPolicy(new iam.PolicyStatement({
      actions: ["iot:Connect", "iot:Subscribe", "iot:Receive", "iot:Publish"],
      resources: ["*"],
    }));

    new cognito.CfnIdentityPoolRoleAttachment(this, "IdentityPoolRoles", {
      identityPoolId: identityPool.ref,
      roles: { authenticated: authRole.roleArn, unauthenticated: unauthRole.roleArn },
    });

    // ========================
    // IoT Core
    // ========================
    const iotEndpoint = new cr.AwsCustomResource(this, "IoTEndpoint", {
      onCreate: {
        service: "Iot",
        action: "describeEndpoint",
        parameters: { endpointType: "iot:Data-ATS" },
        physicalResourceId: cr.PhysicalResourceId.fromResponse("endpointAddress"),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: cr.AwsCustomResourcePolicy.ANY_RESOURCE }),
    });
    const iotEndpointAddress = iotEndpoint.getResponseField("endpointAddress");

    for (const deviceType of ["led_matrix", "rice_cooker", "fan", "oven"]) {
      new iot.CfnThing(this, `IoTThing-${deviceType}`, { thingName: `smarthome-${deviceType}` });
    }

    // ========================
    // Lambda - IoT Control (AgentCore Gateway target)
    // ========================
    const iotControlLambda = new lambda.Function(this, "IoTControlLambda", {
      functionName: "smarthome-iot-control",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../lambda/iot-control")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: { IOT_ENDPOINT: iotEndpointAddress, AWS_IOT_REGION: cdk.Aws.REGION },
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    iotControlLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ["iot:Publish"],
      resources: [`arn:aws:iot:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:topic/smarthome/*`],
    }));

    iotControlLambda.addPermission("AgentCoreGatewayInvoke", {
      principal: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    });

    // ========================
    // S3 + CloudFront - Device Simulator
    // ========================
    const deviceSimBucket = new s3.Bucket(this, "DeviceSimBucket", {
      bucketName: `smarthome-device-sim-${cdk.Aws.ACCOUNT_ID}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });
    const deviceSimOAI = new cloudfront.OriginAccessIdentity(this, "DeviceSimOAI");
    deviceSimBucket.grantRead(deviceSimOAI);

    const deviceSimDistribution = new cloudfront.Distribution(this, "DeviceSimDistribution", {
      defaultBehavior: {
        origin: new origins.S3Origin(deviceSimBucket, { originAccessIdentity: deviceSimOAI }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
      defaultRootObject: "index.html",
      errorResponses: [{ httpStatus: 404, responsePagePath: "/index.html", responseHttpStatus: 200 }],
    });

    // ========================
    // S3 + CloudFront - Chatbot
    // ========================
    const chatbotBucket = new s3.Bucket(this, "ChatbotBucket", {
      bucketName: `smarthome-chatbot-${cdk.Aws.ACCOUNT_ID}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });
    const chatbotOAI = new cloudfront.OriginAccessIdentity(this, "ChatbotOAI");
    chatbotBucket.grantRead(chatbotOAI);

    const chatbotDistribution = new cloudfront.Distribution(this, "ChatbotDistribution", {
      defaultBehavior: {
        origin: new origins.S3Origin(chatbotBucket, { originAccessIdentity: chatbotOAI }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
      defaultRootObject: "index.html",
      errorResponses: [{ httpStatus: 404, responsePagePath: "/index.html", responseHttpStatus: 200 }],
    });

    // Update Cognito callback URLs with CloudFront
    const cfnClient = userPoolClient.node.defaultChild as cognito.CfnUserPoolClient;
    cfnClient.callbackUrLs = [
      "http://localhost:3000/callback",
      `https://${chatbotDistribution.distributionDomainName}/callback`,
    ];
    cfnClient.logoutUrLs = [
      "http://localhost:3000/",
      `https://${chatbotDistribution.distributionDomainName}/`,
    ];

    // ========================
    // Config injection
    // ========================
    const deviceSimConfig = `window.__CONFIG__ = {
  iotEndpoint: "${iotEndpointAddress}",
  region: "${cdk.Aws.REGION}",
  cognitoIdentityPoolId: "${identityPool.ref}"
};`;

    new cr.AwsCustomResource(this, "WriteDeviceSimConfig", {
      onCreate: {
        service: "S3", action: "putObject",
        parameters: { Bucket: deviceSimBucket.bucketName, Key: "config.js", Body: deviceSimConfig, ContentType: "application/javascript" },
        physicalResourceId: cr.PhysicalResourceId.of("device-sim-config"),
      },
      onUpdate: {
        service: "S3", action: "putObject",
        parameters: { Bucket: deviceSimBucket.bucketName, Key: "config.js", Body: deviceSimConfig, ContentType: "application/javascript" },
        physicalResourceId: cr.PhysicalResourceId.of("device-sim-config"),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: [`${deviceSimBucket.bucketArn}/*`] }),
    });

    // Chatbot config.js placeholder — updated by scripts/setup-agentcore.py with runtime ARN
    const chatbotPlaceholder = `window.__CONFIG__ = {
  cognitoUserPoolId: "${userPool.userPoolId}",
  cognitoClientId: "${userPoolClient.userPoolClientId}",
  cognitoDomain: "${userPoolDomain.domainName}.auth.${cdk.Aws.REGION}.amazoncognito.com",
  agentRuntimeArn: "PLACEHOLDER_SET_BY_SETUP_SCRIPT",
  region: "${cdk.Aws.REGION}"
};`;

    new cr.AwsCustomResource(this, "WriteChatbotConfig", {
      onCreate: {
        service: "S3", action: "putObject",
        parameters: { Bucket: chatbotBucket.bucketName, Key: "config.js", Body: chatbotPlaceholder, ContentType: "application/javascript" },
        physicalResourceId: cr.PhysicalResourceId.of("chatbot-config"),
      },
      onUpdate: {
        service: "S3", action: "putObject",
        parameters: { Bucket: chatbotBucket.bucketName, Key: "config.js", Body: chatbotPlaceholder, ContentType: "application/javascript" },
        physicalResourceId: cr.PhysicalResourceId.of("chatbot-config"),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: [`${chatbotBucket.bucketArn}/*`] }),
    });

    // ========================
    // Deploy static assets
    // ========================
    new s3deploy.BucketDeployment(this, "DeployDeviceSim", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../device-simulator/dist"))],
      destinationBucket: deviceSimBucket,
      distribution: deviceSimDistribution,
      distributionPaths: ["/*"],
    });

    new s3deploy.BucketDeployment(this, "DeployChatbot", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../chatbot/dist"))],
      destinationBucket: chatbotBucket,
      distribution: chatbotDistribution,
      distributionPaths: ["/*"],
    });

    // ========================
    // Outputs (consumed by scripts/setup-agentcore.py)
    // ========================
    new cdk.CfnOutput(this, "IoTEndpointOutput", { value: iotEndpointAddress });
    new cdk.CfnOutput(this, "UserPoolId", { value: userPool.userPoolId });
    new cdk.CfnOutput(this, "UserPoolClientId", { value: userPoolClient.userPoolClientId });
    new cdk.CfnOutput(this, "IdentityPoolId", { value: identityPool.ref });
    new cdk.CfnOutput(this, "CognitoDomain", {
      value: `${userPoolDomain.domainName}.auth.${cdk.Aws.REGION}.amazoncognito.com`,
    });
    new cdk.CfnOutput(this, "IoTControlLambdaArn", { value: iotControlLambda.functionArn });
    new cdk.CfnOutput(this, "ChatbotBucketName", { value: chatbotBucket.bucketName });
    new cdk.CfnOutput(this, "ChatbotDistributionId", { value: chatbotDistribution.distributionId });
    new cdk.CfnOutput(this, "DeviceSimulatorUrl", {
      value: `https://${deviceSimDistribution.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, "ChatbotUrl", {
      value: `https://${chatbotDistribution.distributionDomainName}`,
    });
  }
}
