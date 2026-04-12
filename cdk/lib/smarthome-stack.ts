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
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as apigw from "aws-cdk-lib/aws-apigateway";
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
    // Lambda - IoT Discovery (AgentCore Gateway target)
    // ========================
    const iotDiscoveryLambda = new lambda.Function(this, "IoTDiscoveryLambda", {
      functionName: "smarthome-iot-discovery",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../lambda/iot-discovery")),
      timeout: cdk.Duration.seconds(10),
      memorySize: 128,
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    iotDiscoveryLambda.addPermission("AgentCoreGatewayInvoke", {
      principal: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    });

    // ========================
    // Cognito Admin Group + Default Admin User
    // ========================
    new cognito.CfnUserPoolGroup(this, "AdminGroup", {
      userPoolId: userPool.userPoolId,
      groupName: "admin",
      description: "Administrators who can manage agent skills",
    });

    const adminUsername = "admin@smarthome.local";
    const adminPassword = "SmartHome#Admin1";

    // Create the admin user with a permanent password
    const adminUser = new cr.AwsCustomResource(this, "CreateAdminUser", {
      onCreate: {
        service: "CognitoIdentityServiceProvider",
        action: "adminCreateUser",
        parameters: {
          UserPoolId: userPool.userPoolId,
          Username: adminUsername,
          TemporaryPassword: adminPassword,
          MessageAction: "SUPPRESS",
          UserAttributes: [
            { Name: "email", Value: `admin@smarthome.local` },
            { Name: "email_verified", Value: "true" },
          ],
        },
        physicalResourceId: cr.PhysicalResourceId.of("admin-user"),
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ["cognito-idp:AdminCreateUser"],
          resources: [userPool.userPoolArn],
        }),
      ]),
    });

    // Set a permanent password (moves user out of FORCE_CHANGE_PASSWORD state)
    const adminSetPassword = new cr.AwsCustomResource(this, "SetAdminPassword", {
      onCreate: {
        service: "CognitoIdentityServiceProvider",
        action: "adminSetUserPassword",
        parameters: {
          UserPoolId: userPool.userPoolId,
          Username: adminUsername,
          Password: adminPassword,
          Permanent: true,
        },
        physicalResourceId: cr.PhysicalResourceId.of("admin-password"),
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ["cognito-idp:AdminSetUserPassword"],
          resources: [userPool.userPoolArn],
        }),
      ]),
    });
    adminSetPassword.node.addDependency(adminUser);

    // Add admin user to admin group
    const adminGroupMembership = new cr.AwsCustomResource(this, "AddAdminToGroup", {
      onCreate: {
        service: "CognitoIdentityServiceProvider",
        action: "adminAddUserToGroup",
        parameters: {
          UserPoolId: userPool.userPoolId,
          Username: adminUsername,
          GroupName: "admin",
        },
        physicalResourceId: cr.PhysicalResourceId.of("admin-group-membership"),
      },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({
          actions: ["cognito-idp:AdminAddUserToGroup"],
          resources: [userPool.userPoolArn],
        }),
      ]),
    });
    adminGroupMembership.node.addDependency(adminUser);

    // ========================
    // DynamoDB - Skills Table
    // ========================
    const skillsTable = new dynamodb.Table(this, "SkillsTable", {
      tableName: `smarthome-skills`,
      partitionKey: { name: "userId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "skillName", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ========================
    // S3 - Skill Files (scripts, references, assets)
    // ========================
    const skillFilesBucket = new s3.Bucket(this, "SkillFilesBucket", {
      bucketName: `smarthome-skill-files-${cdk.Aws.ACCOUNT_ID}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      cors: [
        {
          allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.PUT],
          allowedOrigins: ["*"],
          allowedHeaders: ["*"],
          maxAge: 3600,
        },
      ],
    });

    // ========================
    // Lambda - Admin API
    // ========================
    const adminLambda = new lambda.Function(this, "AdminLambda", {
      functionName: "smarthome-admin-api",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../lambda/admin-api")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        SKILLS_TABLE_NAME: skillsTable.tableName,
        SKILL_FILES_BUCKET: skillFilesBucket.bucketName,
        AGENT_RUNTIME_ARN: "PLACEHOLDER_SET_BY_SETUP_SCRIPT",
        COGNITO_USER_POOL_ID: userPool.userPoolId,
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
    });
    skillsTable.grantReadWriteData(adminLambda);
    skillFilesBucket.grantReadWrite(adminLambda);

    // ========================
    // API Gateway - Admin API
    // ========================
    const adminApi = new apigw.RestApi(this, "AdminApi", {
      restApiName: "smarthome-admin-api",
      defaultCorsPreflightOptions: {
        allowOrigins: apigw.Cors.ALL_ORIGINS,
        allowMethods: apigw.Cors.ALL_METHODS,
        allowHeaders: ["Content-Type", "Authorization"],
      },
    });

    const cognitoAuthorizer = new apigw.CognitoUserPoolsAuthorizer(this, "AdminAuthorizer", {
      cognitoUserPools: [userPool],
    });
    const authMethodOptions: apigw.MethodOptions = {
      authorizer: cognitoAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    };

    const adminIntegration = new apigw.LambdaIntegration(adminLambda);

    // /users (Cognito users)
    const usersApiResource = adminApi.root.addResource("users");
    usersApiResource.addMethod("GET", adminIntegration, authMethodOptions);

    // /users/{userId}/permissions
    const userIdPermResource = usersApiResource.addResource("{userId}");
    const permissionsResource = userIdPermResource.addResource("permissions");
    permissionsResource.addMethod("GET", adminIntegration, authMethodOptions);
    permissionsResource.addMethod("PUT", adminIntegration, authMethodOptions);

    // /tools (Gateway tools)
    const toolsResource = adminApi.root.addResource("tools");
    toolsResource.addMethod("GET", adminIntegration, authMethodOptions);

    // /memories
    const memoriesResource = adminApi.root.addResource("memories");
    memoriesResource.addMethod("GET", adminIntegration, authMethodOptions);

    // /memories/{actorId}
    const memoryActorResource = memoriesResource.addResource("{actorId}");
    memoryActorResource.addMethod("GET", adminIntegration, authMethodOptions);

    // /skills
    const skillsResource = adminApi.root.addResource("skills");
    skillsResource.addMethod("GET", adminIntegration, authMethodOptions);
    skillsResource.addMethod("POST", adminIntegration, authMethodOptions);

    // /skills/users
    const skillUsersResource = skillsResource.addResource("users");
    skillUsersResource.addMethod("GET", adminIntegration, authMethodOptions);

    // /skills/{userId}/{skillName}
    const userIdResource = skillsResource.addResource("{userId}");
    const skillNameResource = userIdResource.addResource("{skillName}");
    skillNameResource.addMethod("GET", adminIntegration, authMethodOptions);
    skillNameResource.addMethod("PUT", adminIntegration, authMethodOptions);
    skillNameResource.addMethod("DELETE", adminIntegration, authMethodOptions);

    // /skills/{userId}/{skillName}/files
    const filesResource = skillNameResource.addResource("files");
    filesResource.addMethod("GET", adminIntegration, authMethodOptions);
    filesResource.addMethod("DELETE", adminIntegration, authMethodOptions);

    // /skills/{userId}/{skillName}/files/upload-url
    const uploadUrlResource = filesResource.addResource("upload-url");
    uploadUrlResource.addMethod("POST", adminIntegration, authMethodOptions);

    // /skills/{userId}/{skillName}/files/download-url
    const downloadUrlResource = filesResource.addResource("download-url");
    downloadUrlResource.addMethod("POST", adminIntegration, authMethodOptions);

    // /settings/{userId}
    const settingsResource = adminApi.root.addResource("settings").addResource("{userId}");
    settingsResource.addMethod("GET", adminIntegration, authMethodOptions);
    settingsResource.addMethod("PUT", adminIntegration, authMethodOptions);

    // /sessions
    const sessionsResource = adminApi.root.addResource("sessions");
    sessionsResource.addMethod("GET", adminIntegration, authMethodOptions);

    // /sessions/{sessionId}/stop
    const sessionIdResource = sessionsResource.addResource("{sessionId}");
    const stopResource = sessionIdResource.addResource("stop");
    stopResource.addMethod("POST", adminIntegration, authMethodOptions);

    // Grant admin Lambda permission to stop runtime sessions and read memory
    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        "bedrock-agentcore:StopRuntimeSession",
        "bedrock-agentcore:ListActors",
        "bedrock-agentcore:ListMemoryRecords",
      ],
      resources: ["*"],
    }));

    // Grant admin Lambda S3 read for gateway tool schemas (stored in CDK assets bucket)
    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ["s3:GetObject"],
      resources: [`arn:aws:s3:::cdk-*-assets-${cdk.Aws.ACCOUNT_ID}-${cdk.Aws.REGION}/*`],
    }));

    // Grant admin Lambda permissions for Cognito user listing
    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        "cognito-idp:ListUsers",
        "cognito-idp:AdminListGroupsForUser",
      ],
      resources: [userPool.userPoolArn],
    }));

    // Grant admin Lambda permissions for AgentCore Gateway tools and policy management
    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        "bedrock-agentcore:CreatePolicyEngine",
        "bedrock-agentcore:GetPolicyEngine",
        "bedrock-agentcore:ListPolicyEngines",
        "bedrock-agentcore:CreatePolicy",
        "bedrock-agentcore:UpdatePolicy",
        "bedrock-agentcore:DeletePolicy",
        "bedrock-agentcore:ListPolicies",
        "bedrock-agentcore:GetPolicy",
        "bedrock-agentcore:ManageAdminPolicy",
        "bedrock-agentcore:ManageResourceScopedPolicy",
        "bedrock-agentcore:GetGateway",
        "bedrock-agentcore:UpdateGateway",
        "bedrock-agentcore:ListGatewayTargets",
        "bedrock-agentcore:GetGatewayTarget",
      ],
      resources: ["*"],
    }));

    // UpdateGateway requires iam:PassRole; gateway role needs policy engine permissions
    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ["iam:PassRole", "iam:PutRolePolicy"],
      resources: ["*"],
      conditions: {
        StringEquals: {
          "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
        },
      },
    }));
    // iam:PutRolePolicy without condition (for granting gateway role policy engine access)
    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ["iam:PutRolePolicy"],
      resources: ["arn:aws:iam::*:role/AgentCore-*"],
    }));

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

    // ========================
    // S3 + CloudFront - Admin Console
    // ========================
    const adminBucket = new s3.Bucket(this, "AdminConsoleBucket", {
      bucketName: `smarthome-admin-console-${cdk.Aws.ACCOUNT_ID}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });
    const adminOAI = new cloudfront.OriginAccessIdentity(this, "AdminConsoleOAI");
    adminBucket.grantRead(adminOAI);

    const adminDistribution = new cloudfront.Distribution(this, "AdminConsoleDistribution", {
      defaultBehavior: {
        origin: new origins.S3Origin(adminBucket, { originAccessIdentity: adminOAI }),
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

    // Admin Console config.js
    const adminConsoleConfig = `window.__CONFIG__ = {
  cognitoUserPoolId: "${userPool.userPoolId}",
  cognitoClientId: "${userPoolClient.userPoolClientId}",
  adminApiUrl: "${adminApi.url}",
  region: "${cdk.Aws.REGION}"
};`;

    new cr.AwsCustomResource(this, "WriteAdminConsoleConfig", {
      onCreate: {
        service: "S3", action: "putObject",
        parameters: { Bucket: adminBucket.bucketName, Key: "config.js", Body: adminConsoleConfig, ContentType: "application/javascript" },
        physicalResourceId: cr.PhysicalResourceId.of("admin-console-config"),
      },
      onUpdate: {
        service: "S3", action: "putObject",
        parameters: { Bucket: adminBucket.bucketName, Key: "config.js", Body: adminConsoleConfig, ContentType: "application/javascript" },
        physicalResourceId: cr.PhysicalResourceId.of("admin-console-config"),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: [`${adminBucket.bucketArn}/*`] }),
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

    new s3deploy.BucketDeployment(this, "DeployAdminConsole", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../admin-console/dist"))],
      destinationBucket: adminBucket,
      distribution: adminDistribution,
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
    new cdk.CfnOutput(this, "IoTDiscoveryLambdaArn", { value: iotDiscoveryLambda.functionArn });
    new cdk.CfnOutput(this, "ChatbotBucketName", { value: chatbotBucket.bucketName });
    new cdk.CfnOutput(this, "ChatbotDistributionId", { value: chatbotDistribution.distributionId });
    new cdk.CfnOutput(this, "DeviceSimBucketName", { value: deviceSimBucket.bucketName });
    new cdk.CfnOutput(this, "DeviceSimDistributionId", { value: deviceSimDistribution.distributionId });
    new cdk.CfnOutput(this, "DeviceSimulatorUrl", {
      value: `https://${deviceSimDistribution.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, "ChatbotUrl", {
      value: `https://${chatbotDistribution.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, "AdminApiUrl", { value: adminApi.url });
    new cdk.CfnOutput(this, "SkillsTableName", { value: skillsTable.tableName });
    new cdk.CfnOutput(this, "AdminConsoleBucketName", { value: adminBucket.bucketName });
    new cdk.CfnOutput(this, "AdminConsoleDistributionId", { value: adminDistribution.distributionId });
    new cdk.CfnOutput(this, "AdminConsoleUrl", {
      value: `https://${adminDistribution.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, "SkillFilesBucketName", { value: skillFilesBucket.bucketName });
    new cdk.CfnOutput(this, "AdminUsername", { value: adminUsername });
    new cdk.CfnOutput(this, "AdminPassword", { value: adminPassword });
  }
}
