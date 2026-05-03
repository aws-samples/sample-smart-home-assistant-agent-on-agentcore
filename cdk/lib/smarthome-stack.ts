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
    // Authenticated users can connect to IoT Core and subscribe/publish on
    // any topic. Per-user isolation at the topic level is NOT enforced by
    // this IAM policy — that would require mapping the Identity Pool
    // identity ID (which is what `cognito-identity.amazonaws.com:sub` resolves
    // to in policy conditions) back to the User Pool `sub` our topic scheme
    // uses, which isn't trivial. Instead:
    //   • The agent→Lambda path is gated: iot-control derives the User Pool
    //     `sub` from the validated JWT and publishes to
    //     `smarthome/<sub>/<device>/command` only. The LLM cannot forge it.
    //   • Device-simulator clients only subscribe to their own
    //     `smarthome/<own-sub>/+` filter, so browser-side isolation holds as
    //     long as the device-simulator client composes topics correctly.
    // A determined user can still subscribe to another user's topics with
    // raw MQTT; closing that would require an IoT Core Policy keyed on a
    // Cognito User Pool custom claim (future hardening).
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

    // AWS IoT Core, even for SigV4 WebSocket MQTT, enforces authorization
    // via an IoT *Policy* (distinct from IAM) — this is a long-standing quirk
    // of connecting authenticated Cognito users. We declare a permissive
    // policy here, and the device-simulator attaches it to each user's
    // Cognito identity at login via iot:AttachPolicy. (The unauthenticated
    // Cognito role happens to pass without this — only the authenticated
    // flow trips the check.)
    new iot.CfnPolicy(this, "DeviceSimClientPolicy", {
      policyName: "smarthome-device-sim-client",
      policyDocument: {
        Version: "2012-10-17",
        Statement: [
          { Effect: "Allow", Action: ["iot:Connect", "iot:Subscribe", "iot:Receive", "iot:Publish"], Resource: "*" },
        ],
      },
    });
    // Allow the authenticated users to self-attach the policy above to their
    // own Cognito identity. iot:AttachPolicy's resource policy binds against
    // both the policy ARN AND the target identity, and the target is a
    // transient `cognito-identity.amazonaws.com:sub` that can't be enumerated
    // in advance, so we use `*` here. The policy document itself
    // (smarthome-device-sim-client) is the least-privileged MQTT policy,
    // limiting blast radius even if someone forced an attach.
    authRole.addToPolicy(new iam.PolicyStatement({
      actions: ["iot:AttachPolicy", "iot:ListAttachedPolicies"],
      resources: ["*"],
    }));

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
    // Lambda - User Init (Cognito Post-Confirmation trigger)
    // Auto-provisions all tool permissions for newly confirmed users.
    // Separate Lambda to avoid circular dependency with admin API's Cognito authorizer.
    // ========================
    const userInitLambda = new lambda.Function(this, "UserInitLambda", {
      functionName: "smarthome-user-init",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../lambda/user-init")),
      timeout: cdk.Duration.seconds(60),
      memorySize: 256,
      environment: {
        SKILLS_TABLE_NAME: skillsTable.tableName,
        GATEWAY_ID: "PLACEHOLDER_SET_BY_SETUP_SCRIPT",
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
    });
    skillsTable.grantReadWriteData(userInitLambda);

    // Grant permissions for AgentCore Gateway tools and policy management
    userInitLambda.addToRolePolicy(new iam.PolicyStatement({
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

    // S3 read for gateway tool schemas stored in CDK assets bucket
    userInitLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ["s3:GetObject"],
      resources: [`arn:aws:s3:::cdk-*-assets-${cdk.Aws.ACCOUNT_ID}-${cdk.Aws.REGION}/*`],
    }));

    // iam:PassRole + PutRolePolicy for gateway policy engine association
    userInitLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ["iam:PassRole", "iam:PutRolePolicy"],
      resources: ["*"],
      conditions: {
        StringEquals: {
          "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
        },
      },
    }));
    userInitLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ["iam:PutRolePolicy"],
      resources: ["arn:aws:iam::*:role/AgentCore-*"],
    }));

    userPool.addTrigger(cognito.UserPoolOperation.POST_CONFIRMATION, userInitLambda);

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

    // NOTE: Per-user & global system prompt overrides for text / voice agents
    // are stored in the existing skills DynamoDB table under reserved sort
    // keys (skillName = "__prompt_text__" / "__prompt_voice__") and served
    // through the EXISTING /skills routes — no new API Gateway methods were
    // added here because the admin Lambda's resource-policy is at the 20 KB
    // cap. The Lambda recognises the reserved sort keys in list/get/put/delete
    // paths and enforces the 16 KB promptBody limit in-line.

    // /sessions
    const sessionsResource = adminApi.root.addResource("sessions");
    sessionsResource.addMethod("GET", adminIntegration, authMethodOptions);

    // /sessions/{sessionId}/stop
    const sessionIdResource = sessionsResource.addResource("{sessionId}");
    const stopResource = sessionIdResource.addResource("stop");
    stopResource.addMethod("POST", adminIntegration, authMethodOptions);

    // /knowledge-bases (consolidated: action-based dispatch to avoid Lambda policy size limit)
    const kbResource = adminApi.root.addResource("knowledge-bases");
    kbResource.addMethod("GET", adminIntegration, authMethodOptions);
    kbResource.addMethod("POST", adminIntegration, authMethodOptions);

    // Grant admin Lambda permission to stop runtime sessions and read memory
    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        "bedrock-agentcore:StopRuntimeSession",
        "bedrock-agentcore:ListActors",
        "bedrock-agentcore:ListMemoryRecords",
      ],
      resources: ["*"],
    }));

    // Grant admin Lambda CloudWatch Logs Insights access on the aws/spans log
    // group (AgentCore GenAI spans). Used to aggregate per-session token usage
    // for the Sessions tab.
    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        "logs:StartQuery",
        "logs:StopQuery",
      ],
      resources: [
        `arn:aws:logs:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:log-group:aws/spans:*`,
      ],
    }));
    // GetQueryResults doesn't support resource-level scoping.
    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ["logs:GetQueryResults"],
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
    // Knowledge Base Infrastructure (S3 Vectors + S3 docs + Bedrock KB)
    // ========================
    //
    // Vector storage moved from OpenSearch Serverless → S3 Vectors. AOSS has a
    // ~$350/month floor; S3 Vectors is pay-per-vector serverless. The S3 vector
    // bucket + index are created imperatively by setup-agentcore.py (no L1 CDK
    // construct for s3vectors exists yet).

    // S3 bucket for KB documents (organized by scope prefix: __shared__/, user@example.com/)
    const kbDocsBucket = new s3.Bucket(this, "KBDocsBucket", {
      bucketName: `smarthome-kb-docs-${cdk.Aws.ACCOUNT_ID}`,
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

    // IAM Role for Bedrock Knowledge Base service
    const kbServiceRole = new iam.Role(this, "KBServiceRole", {
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com"),
    });
    kbServiceRole.addToPolicy(new iam.PolicyStatement({
      actions: ["s3:GetObject", "s3:ListBucket"],
      resources: [kbDocsBucket.bucketArn, `${kbDocsBucket.bucketArn}/*`],
    }));
    kbServiceRole.addToPolicy(new iam.PolicyStatement({
      // S3 Vectors permissions for the Bedrock KB service role. Bedrock writes
      // vectors via PutVectors during ingestion and reads via QueryVectors
      // during Retrieve / RetrieveAndGenerate.
      actions: [
        "s3vectors:PutVectors",
        "s3vectors:QueryVectors",
        "s3vectors:GetVectors",
        "s3vectors:DeleteVectors",
        "s3vectors:ListVectors",
        "s3vectors:GetIndex",
        "s3vectors:ListIndexes",
        "s3vectors:GetVectorBucket",
      ],
      resources: ["*"],
    }));
    kbServiceRole.addToPolicy(new iam.PolicyStatement({
      actions: ["bedrock:InvokeModel"],
      resources: [`arn:aws:bedrock:${cdk.Aws.REGION}::foundation-model/cohere.embed-multilingual-v3`],
    }));

    // Lambda - KB Query (AgentCore Gateway target for agent to query knowledge base)
    const kbQueryLambda = new lambda.Function(this, "KBQueryLambda", {
      functionName: "smarthome-kb-query",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../lambda/kb-query")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        SKILLS_TABLE_NAME: skillsTable.tableName,
        AWS_KB_REGION: cdk.Aws.REGION,
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
    });
    skillsTable.grantReadData(kbQueryLambda);
    kbQueryLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
      resources: ["*"],
    }));
    kbQueryLambda.addPermission("AgentCoreGatewayInvoke", {
      principal: new iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
    });

    // Grant admin Lambda KB-related permissions
    adminLambda.addEnvironment("KB_DOCS_BUCKET", kbDocsBucket.bucketName);
    adminLambda.addEnvironment("KB_SERVICE_ROLE_ARN", kbServiceRole.roleArn);
    kbDocsBucket.grantReadWrite(adminLambda);

    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        "bedrock:CreateKnowledgeBase",
        "bedrock:DeleteKnowledgeBase",
        "bedrock:GetKnowledgeBase",
        "bedrock:ListKnowledgeBases",
        "bedrock:CreateDataSource",
        "bedrock:DeleteDataSource",
        "bedrock:GetDataSource",
        "bedrock:StartIngestionJob",
        "bedrock:GetIngestionJob",
        "bedrock:ListIngestionJobs",
      ],
      resources: ["*"],
    }));

    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: ["iam:PassRole"],
      resources: [kbServiceRole.roleArn],
    }));

    // Grant user-init Lambda access to KB docs bucket (create user folder on signup)
    userInitLambda.addEnvironment("KB_DOCS_BUCKET", kbDocsBucket.bucketName);
    kbDocsBucket.grantWrite(userInitLambda);

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
    // Lambda - Skill ERP API (user-scoped CRUD for AgentCore Registry records)
    // ========================
    const skillErpLambda = new lambda.Function(this, "SkillErpLambda", {
      functionName: "smarthome-skill-erp-api",
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: "index.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../lambda/skill-erp-api")),
      timeout: cdk.Duration.seconds(30),
      memorySize: 256,
      environment: {
        SKILLS_TABLE_NAME: skillsTable.tableName,
        REGISTRY_ID: "PLACEHOLDER_SET_BY_SETUP_SCRIPT",
      },
      logRetention: logs.RetentionDays.ONE_WEEK,
    });
    skillsTable.grantReadWriteData(skillErpLambda);
    skillErpLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        "bedrock-agentcore:CreateRegistryRecord",
        "bedrock-agentcore:GetRegistryRecord",
        "bedrock-agentcore:ListRegistryRecords",
        "bedrock-agentcore:UpdateRegistryRecord",
        "bedrock-agentcore:DeleteRegistryRecord",
        "bedrock-agentcore:SubmitRegistryRecordForApproval",
        "bedrock-agentcore:GetRegistry",
      ],
      resources: ["*"],
    }));

    // API Gateway - Skill ERP API (separate API so admin and end-user surfaces
    // stay isolated and we can give skill-erp Cognito users their own scope).
    const skillErpApi = new apigw.RestApi(this, "SkillErpApi", {
      restApiName: "smarthome-skill-erp-api",
      defaultCorsPreflightOptions: {
        allowOrigins: apigw.Cors.ALL_ORIGINS,
        allowMethods: apigw.Cors.ALL_METHODS,
        allowHeaders: ["Content-Type", "Authorization"],
      },
    });
    const skillErpAuthorizer = new apigw.CognitoUserPoolsAuthorizer(this, "SkillErpAuthorizer", {
      cognitoUserPools: [userPool],
    });
    const skillErpAuthOpts: apigw.MethodOptions = {
      authorizer: skillErpAuthorizer,
      authorizationType: apigw.AuthorizationType.COGNITO,
    };
    const skillErpIntegration = new apigw.LambdaIntegration(skillErpLambda);
    const mySkillsResource = skillErpApi.root.addResource("my-skills");
    mySkillsResource.addMethod("GET", skillErpIntegration, skillErpAuthOpts);
    mySkillsResource.addMethod("POST", skillErpIntegration, skillErpAuthOpts);
    const mySkillRecordResource = mySkillsResource.addResource("{recordId}");
    mySkillRecordResource.addMethod("GET", skillErpIntegration, skillErpAuthOpts);
    mySkillRecordResource.addMethod("PUT", skillErpIntegration, skillErpAuthOpts);
    mySkillRecordResource.addMethod("DELETE", skillErpIntegration, skillErpAuthOpts);

    const myA2aResource = skillErpApi.root.addResource("my-a2a-agents");
    myA2aResource.addMethod("GET", skillErpIntegration, skillErpAuthOpts);
    myA2aResource.addMethod("POST", skillErpIntegration, skillErpAuthOpts);
    const myA2aRecordResource = myA2aResource.addResource("{recordId}");
    myA2aRecordResource.addMethod("GET", skillErpIntegration, skillErpAuthOpts);
    myA2aRecordResource.addMethod("PUT", skillErpIntegration, skillErpAuthOpts);
    myA2aRecordResource.addMethod("DELETE", skillErpIntegration, skillErpAuthOpts);

    // Grant admin Lambda Registry access (used by Skills tab "Import from Registry")
    adminLambda.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        "bedrock-agentcore:CreateRegistry",
        "bedrock-agentcore:GetRegistry",
        "bedrock-agentcore:ListRegistries",
        "bedrock-agentcore:UpdateRegistry",
        "bedrock-agentcore:GetRegistryRecord",
        "bedrock-agentcore:ListRegistryRecords",
        "bedrock-agentcore:UpdateRegistryRecordStatus",
      ],
      resources: ["*"],
    }));
    adminLambda.addEnvironment("REGISTRY_ID", "PLACEHOLDER_SET_BY_SETUP_SCRIPT");

    // API Gateway routes for Registry import (admin-only)
    const registryResource = adminApi.root.addResource("registry");
    const registryRecordsResource = registryResource.addResource("records");
    registryRecordsResource.addMethod("GET", adminIntegration, authMethodOptions);
    const registryImportResource = registryResource.addResource("import");
    registryImportResource.addMethod("POST", adminIntegration, authMethodOptions);
    // A2A agents listing reuses the existing /registry/records GET with an
    // ?action=a2a-list query param — adding a new resource would exceed the
    // admin Lambda's 20KB resource-policy cap.

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

    // ========================
    // S3 + CloudFront - Skill ERP (user-facing Registry publisher)
    // ========================
    const skillErpBucket = new s3.Bucket(this, "SkillErpBucket", {
      bucketName: `smarthome-skill-erp-${cdk.Aws.ACCOUNT_ID}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });
    const skillErpOAI = new cloudfront.OriginAccessIdentity(this, "SkillErpOAI");
    skillErpBucket.grantRead(skillErpOAI);

    const skillErpDistribution = new cloudfront.Distribution(this, "SkillErpDistribution", {
      defaultBehavior: {
        origin: new origins.S3Origin(skillErpBucket, { originAccessIdentity: skillErpOAI }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      },
      defaultRootObject: "index.html",
      errorResponses: [{ httpStatus: 404, responsePagePath: "/index.html", responseHttpStatus: 200 }],
    });

    // Update Cognito callback URLs with CloudFront (chatbot + skill-erp)
    const cfnClient = userPoolClient.node.defaultChild as cognito.CfnUserPoolClient;
    cfnClient.callbackUrLs = [
      "http://localhost:3000/callback",
      "http://localhost:3003/callback",
      `https://${chatbotDistribution.distributionDomainName}/callback`,
      `https://${skillErpDistribution.distributionDomainName}/callback`,
    ];
    cfnClient.logoutUrLs = [
      "http://localhost:3000/",
      "http://localhost:3003/",
      `https://${chatbotDistribution.distributionDomainName}/`,
      `https://${skillErpDistribution.distributionDomainName}/`,
    ];

    // ========================
    // Config injection
    // ========================
    const deviceSimConfig = `window.__CONFIG__ = {
  iotEndpoint: "${iotEndpointAddress}",
  region: "${cdk.Aws.REGION}",
  cognitoIdentityPoolId: "${identityPool.ref}",
  cognitoUserPoolId: "${userPool.userPoolId}",
  cognitoClientId: "${userPoolClient.userPoolClientId}"
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
  cognitoIdentityPoolId: "${identityPool.ref}",
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

    // Skill ERP config.js
    const skillErpConfig = `window.__CONFIG__ = {
  cognitoUserPoolId: "${userPool.userPoolId}",
  cognitoClientId: "${userPoolClient.userPoolClientId}",
  erpApiUrl: "${skillErpApi.url}",
  region: "${cdk.Aws.REGION}"
};`;

    new cr.AwsCustomResource(this, "WriteSkillErpConfig", {
      onCreate: {
        service: "S3", action: "putObject",
        parameters: { Bucket: skillErpBucket.bucketName, Key: "config.js", Body: skillErpConfig, ContentType: "application/javascript" },
        physicalResourceId: cr.PhysicalResourceId.of("skill-erp-config"),
      },
      onUpdate: {
        service: "S3", action: "putObject",
        parameters: { Bucket: skillErpBucket.bucketName, Key: "config.js", Body: skillErpConfig, ContentType: "application/javascript" },
        physicalResourceId: cr.PhysicalResourceId.of("skill-erp-config"),
      },
      policy: cr.AwsCustomResourcePolicy.fromSdkCalls({ resources: [`${skillErpBucket.bucketArn}/*`] }),
    });

    // ========================
    // Deploy static assets
    // ========================
    // prune:false preserves config.js — that file is written by a sibling
    // AwsCustomResource and would otherwise be deleted on every deploy
    // because it's not part of the dist/ sources below.
    new s3deploy.BucketDeployment(this, "DeployDeviceSim", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../device-simulator/dist"))],
      destinationBucket: deviceSimBucket,
      distribution: deviceSimDistribution,
      distributionPaths: ["/*"],
      prune: false,
    });

    new s3deploy.BucketDeployment(this, "DeployChatbot", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../chatbot/dist"))],
      destinationBucket: chatbotBucket,
      distribution: chatbotDistribution,
      distributionPaths: ["/*"],
      prune: false,
    });

    new s3deploy.BucketDeployment(this, "DeployAdminConsole", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../admin-console/dist"))],
      destinationBucket: adminBucket,
      distribution: adminDistribution,
      distributionPaths: ["/*"],
      prune: false,
    });

    new s3deploy.BucketDeployment(this, "DeploySkillErp", {
      sources: [s3deploy.Source.asset(path.join(__dirname, "../../skill-erp/dist"))],
      destinationBucket: skillErpBucket,
      distribution: skillErpDistribution,
      distributionPaths: ["/*"],
      prune: false,
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
    new cdk.CfnOutput(this, "KBDocsBucketName", { value: kbDocsBucket.bucketName });
    new cdk.CfnOutput(this, "KBQueryLambdaArn", { value: kbQueryLambda.functionArn });
    new cdk.CfnOutput(this, "KBServiceRoleArn", { value: kbServiceRole.roleArn });
    new cdk.CfnOutput(this, "CognitoAuthRoleArn", { value: authRole.roleArn });
    new cdk.CfnOutput(this, "SkillErpBucketName", { value: skillErpBucket.bucketName });
    new cdk.CfnOutput(this, "SkillErpDistributionId", { value: skillErpDistribution.distributionId });
    new cdk.CfnOutput(this, "SkillErpUrl", {
      value: `https://${skillErpDistribution.distributionDomainName}`,
    });
    new cdk.CfnOutput(this, "SkillErpApiUrl", { value: skillErpApi.url });
  }
}
