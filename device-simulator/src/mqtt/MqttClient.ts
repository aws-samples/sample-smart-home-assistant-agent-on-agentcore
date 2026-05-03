import { mqtt5, auth, iot } from "aws-iot-device-sdk-v2";
import { fromCognitoIdentityPool } from "@aws-sdk/credential-providers";
import { toUtf8 } from "@aws-sdk/util-utf8-browser";
import { getConfig } from "../config";

type MessageCallback = (topic: string, payload: any) => void;

export class MqttClient {
  private static instance: MqttClient;
  private client: mqtt5.Mqtt5Client | null = null;
  private subscriptions: Map<string, MessageCallback[]> = new Map();
  private connected: boolean = false;
  private connectionListeners: Array<(connected: boolean) => void> = [];

  private constructor() {}

  static getInstance(): MqttClient {
    if (!MqttClient.instance) {
      MqttClient.instance = new MqttClient();
    }
    return MqttClient.instance;
  }

  isConnected(): boolean {
    return this.connected;
  }

  onConnectionChange(listener: (connected: boolean) => void) {
    this.connectionListeners.push(listener);
    return () => {
      this.connectionListeners = this.connectionListeners.filter(
        (l) => l !== listener
      );
    };
  }

  private notifyConnectionChange(connected: boolean) {
    this.connected = connected;
    this.connectionListeners.forEach((l) => l(connected));
  }

  /**
   * idToken — optional Cognito User Pool idToken. When supplied, Identity Pool
   * credentials are federated from the User Pool login (authenticated role),
   * so the MQTT client subscribes/publishes as the signed-in user. When
   * omitted the client falls back to the unauthenticated Identity Pool role
   * (pre-auth visitors). After this change the device simulator always signs
   * in first, so the token path is the only one exercised in practice; the
   * unauth path is left as a defensive fallback.
   */
  async connect(idToken?: string): Promise<void> {
    const config = getConfig();

    if (!config.cognitoIdentityPoolId) {
      console.warn(
        "No Cognito Identity Pool ID configured. MQTT connection skipped."
      );
      return;
    }

    // Fetch temporary credentials from Cognito Identity Pool — authenticated
    // if we have an idToken, otherwise the unauthenticated role.
    const logins = idToken && config.cognitoUserPoolId
      ? { [`cognito-idp.${config.region}.amazonaws.com/${config.cognitoUserPoolId}`]: idToken }
      : undefined;
    const credentialsProvider = fromCognitoIdentityPool({
      identityPoolId: config.cognitoIdentityPoolId,
      clientConfig: { region: config.region },
      logins,
    });

    const creds = await credentialsProvider();

    // Create a static credentials provider (browser API uses StaticCredentialProvider)
    const StaticCredentialProvider = (auth as any).StaticCredentialProvider;
    const staticProvider = new StaticCredentialProvider({
      aws_access_id: creds.accessKeyId,
      aws_secret_key: creds.secretAccessKey,
      aws_sts_token: creds.sessionToken,
      aws_region: config.region,
    });

    const builder =
      iot.AwsIotMqtt5ClientConfigBuilder.newWebsocketMqttBuilderWithSigv4Auth(
        config.iotEndpoint,
        {
          credentialsProvider: staticProvider,
          region: config.region,
        }
      );

    builder.withConnectProperties({
      clientId: "device-sim-" + Math.floor(Math.random() * 100000000),
      keepAliveIntervalSeconds: 30,
    });

    this.client = new mqtt5.Mqtt5Client(builder.build());

    this.client.on("connectionSuccess", () => {
      console.log("[MQTT] Connected to", config.iotEndpoint);
      this.notifyConnectionChange(true);

      // Re-subscribe to all topics
      this.subscriptions.forEach((_, topic) => {
        console.log("[MQTT] Subscribing to:", topic);
        this.client?.subscribe({
          subscriptions: [{ topicFilter: topic, qos: mqtt5.QoS.AtLeastOnce }],
        });
      });
    });

    this.client.on("disconnection", () => {
      console.log("[MQTT] Disconnected");
      this.notifyConnectionChange(false);
    });

    this.client.on("connectionFailure", (eventData: any) => {
      console.error("[MQTT] Connection failed:", eventData);
      this.notifyConnectionChange(false);
    });

    this.client.on("messageReceived", (eventData: any) => {
      const topic = eventData.message?.topicName;
      const payloadBytes = eventData.message?.payload;
      if (topic && payloadBytes) {
        try {
          const payloadStr = toUtf8(new Uint8Array(payloadBytes));
          const payload = JSON.parse(payloadStr);
          console.log("[MQTT] Received:", topic, payload);
          const callbacks = this.subscriptions.get(topic) || [];
          callbacks.forEach((cb) => cb(topic, payload));
        } catch (e) {
          console.error("[MQTT] Failed to parse message:", e);
        }
      }
    });

    this.client.start();

    // Refresh credentials every 45 minutes (they expire in ~1 hour)
    setInterval(async () => {
      try {
        console.log("Refreshing Cognito credentials...");
        await credentialsProvider();
        // Reconnect with new credentials — re-thread the current idToken so
        // the refreshed session stays authenticated as the same user.
        if (this.client) {
          this.client.stop();
          this.client = null;
        }
        await this.connect(idToken);
      } catch (e) {
        console.error("Failed to refresh credentials:", e);
      }
    }, 45 * 60 * 1000);
  }

  subscribe(topic: string, callback: MessageCallback): void {
    if (!this.subscriptions.has(topic)) {
      this.subscriptions.set(topic, []);
    }
    this.subscriptions.get(topic)!.push(callback);

    if (this.client && this.connected) {
      console.log("[MQTT] Subscribing to:", topic);
      this.client.subscribe({
        subscriptions: [{ topicFilter: topic, qos: mqtt5.QoS.AtLeastOnce }],
      });
    }
  }

  unsubscribe(topic: string, callback: MessageCallback): void {
    const callbacks = this.subscriptions.get(topic);
    if (callbacks) {
      const idx = callbacks.indexOf(callback);
      if (idx >= 0) callbacks.splice(idx, 1);
      if (callbacks.length === 0) {
        this.subscriptions.delete(topic);
        if (this.client && this.connected) {
          this.client.unsubscribe({ topicFilters: [topic] });
        }
      }
    }
  }

  async publish(topic: string, payload: any): Promise<void> {
    if (!this.client || !this.connected) {
      console.warn("MQTT not connected, cannot publish");
      return;
    }
    const encoder = new TextEncoder();
    this.client.publish({
      topicName: topic,
      payload: encoder.encode(JSON.stringify(payload)),
      qos: mqtt5.QoS.AtLeastOnce,
    });
  }

  async disconnect(): Promise<void> {
    if (this.client) {
      this.client.stop();
      this.client = null;
      this.notifyConnectionChange(false);
    }
  }
}

export default MqttClient;
