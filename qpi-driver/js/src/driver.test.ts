import * as http from "node:http";
import * as tls from "node:tls";
import type { AddressInfo } from "node:net";

import { QpiDriver } from "./driver.js";
import { Event, EventType } from "./events.js";
import {
  PipelineSocket,
  MessageAssembler,
  buildSpHeader,
  frame,
  PROTO_PUSH,
  PROTO_PULL,
} from "./nng.js";

const CERT = `-----BEGIN CERTIFICATE-----
MIIDJTCCAg2gAwIBAgIUQSee5yPHN73N+oCeDNg8ifaBqnIwDQYJKoZIhvcNAQEL
BQAwFDESMBAGA1UEAwwJbG9jYWxob3N0MB4XDTI2MDcyMzA2NDMwNloXDTM2MDcy
MDA2NDMwNlowFDESMBAGA1UEAwwJbG9jYWxob3N0MIIBIjANBgkqhkiG9w0BAQEF
AAOCAQ8AMIIBCgKCAQEAnZXXiBzhAl2IMXRacRgNJWiMl2NPFuS/UIs5B0yymCBJ
R4UckvSEp63fNUxjlIrnHF5R+LMDO/Vdd36Wnd9EzXWBDdNSATMH68Iyaza7g+ET
Dw4revMvawNqL0HVNtdrd6NP8FKxfuX0t1VK9lJr3P3q+XX9kIwGZZIASoFaYIiF
7AR02WfnBqUUmduJCa9iLAKbBfvyc/KiGl/T5Hga4hJJZk3pT8VVy5TpEH10WUV2
2j3SzpVZ6KLcF/4TmOSH49gOKDWWEugbwZKBvjvUSnaVcVqcZibpYXWucxnHtwNd
FnW0B652TBaM+uRa0JQh4/l4JaxE6bcDMf8RXVykzwIDAQABo28wbTAdBgNVHQ4E
FgQUu/9c7OxJ5fpvcTcE1cTYMkXdkTEwHwYDVR0jBBgwFoAUu/9c7OxJ5fpvcTcE
1cTYMkXdkTEwDwYDVR0TAQH/BAUwAwEB/zAaBgNVHREEEzARgglsb2NhbGhvc3SH
BH8AAAEwDQYJKoZIhvcNAQELBQADggEBABTz6lvMRHuteSTc4joazzMsupZ9SojL
ReZ3/JAq0jhkK4dyRNSw+VlMHQ5tQNkgBmJUkEIYLTwj9H6iNVLKG41IPbGR5+Q6
/fW73T2k7MhgStcFahy+oFfsV4RM9HpPdDZG6+PMRncdG/gpmMzRSiMPSibln0XY
kbXOHsD4ih1U7dcD5VLPjbD5TTX6Tyx9IBQ9QG/XjB3V7Yzx492T2LKWRkxjxV72
G7Aw+/5gG3gppXyFJYllnoqYx+0RIpi9QDAy6LxhFwkNdria4jp944quARO8ZQVK
soXyQldPcvIUn/pKWTVWF1fDuHAgVVzzOZ4+Uy7s9xls79xm0Y4k3kY=
-----END CERTIFICATE-----
`;
const KEY = `-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCdldeIHOECXYgx
dFpxGA0laIyXY08W5L9QizkHTLKYIElHhRyS9ISnrd81TGOUiuccXlH4swM79V13
fpad30TNdYEN01IBMwfrwjJrNruD4RMPDit68y9rA2ovQdU212t3o0/wUrF+5fS3
VUr2Umvc/er5df2QjAZlkgBKgVpgiIXsBHTZZ+cGpRSZ24kJr2IsApsF+/Jz8qIa
X9PkeBriEklmTelPxVXLlOkQfXRZRXbaPdLOlVnootwX/hOY5Ifj2A4oNZYS6BvB
koG+O9RKdpVxWpxmJulhda5zGce3A10WdbQHrnZMFoz65FrQlCHj+XglrETptwMx
/xFdXKTPAgMBAAECggEAGi8fKHkv9A4phpogOwF1kb0s+yyfpByvI0l22N0oIbnf
ozgddteVQS1VVMxUEYcA/sg3U50fEPPkq2nHygSisIbxQiRWUGezzbsvWHw4LSIV
Yh+HHv9QZjYjiyWjsWCa9T6YFkUPRBgekOXltkccsBQq2nd+AeoaV/8p+DdFFpvl
zJ1BazPl2P+SfVIFDe6MOFtEvF7Q9SkzZUSJ72fY+hT2YFKKjI0LyWPN2MubgVpy
uZqliJeSF8EdjSNEptYk9CjRByqrCiVcoTMv/FKVyJD/9gns/tiD2YbIRGKSU8C1
btz+e17423MSKy9rE6gH46wuAzxJ1sL40tE1QDE8xQKBgQDWdtHfO8h9Wkq852hn
18fqpWSGmR9ZvUuFEtzgK6OdUxIZQwsrf0VS4eEo0f88dkCCr14t+dSZxx+xQRZp
ybjXQpe4ulxtUiIkQGPpSxsWqjwkZ4CpQZ277MaA2HK7FfW6zBA61dYYMuaQTRJ4
u6Iq5brl4S1W6jvzLPAELXbiXQKBgQC8GvUboQws6r9TRKa5DT1aYIiLIU2MjFVE
EQvB2WUbrlbpzeDdgJRQq4fguCQN/rC+PKM5CYcmPvDLOQ8Lr6Bkm1UcdKlHS6F/
gIcvM0CpzelAwxef4S7B/ubUGhMRUsGOCTg+92Q0bkSN9TbcAMWjQu+Nu3qdMl7B
gUpu8HuJGwKBgFw7o4zG8DWA1G2jc9JdCZxPXwlH5yS39TeY4icCfY4WgM0eeTpO
tOitPiFJFuTQ0nOhqfZJ4HX2HhokLNh4Kadh+1A1zbQyQ36ltpJJe6/mrJDXdozU
LFr6vHADJmxxSEn6ouw6tKWZlnDuxIfp4hdiz1s32UDs4bV2WQ7i4qL9AoGBAIjF
gRsJynSOa3b8H83F1qp0LlQbbuuWzhij3Eyi6WVrKj7uN2ZXK4BMeIvo2C5k1dY8
+OFsEBy6/xKE9m+kz5bXatc57CuuzkqLBcBIH+hXlBZGxFK3xOvBj80A+IRMC/he
s8r0zqNg2e/uMGlfFlVTQiNoAgtyqHtCqwBnUyupAoGBAMO8x70eCYZlS85vJKT5
Nm9sX8dhbOdIxBG/tA5fD0H7XTwkfwWb6yBw/PJ2IvPVI7y2hv4BrW1gg7AH+TDr
E1mY2M+So0whlmduJW6bKwyTIXFKBZRt4xBlRgnUqx4Ih43wFMoTO+TT7d5/nUpL
04fM5BV8FEn1m0j/GQqmbnyv
-----END PRIVATE KEY-----
`;
const FINGERPRINT =
  "faf36b68aef4fd1e4300c9c55f4eaf239c3e2395b58340f83d02032f65176147";

class EchoDriver extends QpiDriver {
  readonly seen: Event[] = [];

  handleEvent(event: Event): void {
    this.seen.push(event);
    if (event.type === EventType.JobDispatch) {
      this.emit(
        new Event(EventType.JobResult, {
          job_id: event.payload.job_id,
          status: "completed",
        }),
      );
    }
  }
}

function tlsPort(server: tls.Server): number {
  return (server.address() as AddressInfo).port;
}

describe("QpiDriver", () => {
  test("emit before run throws", () => {
    const driver = new EchoDriver({
      qpiAddr: "http://127.0.0.1:1",
      token: "t",
      name: "qpu_1",
      caFingerprint: FINGERPRINT,
    });
    expect(() => driver.emit(new Event(EventType.JobResult))).toThrow(
      /before the driver is running/,
    );
  });

  test("round-trips an inbound JobDispatch into an emitted JobResult", async () => {
    // Outbound endpoint: acts as the server's PULL, collecting what the driver emits.
    let resolveEmitted!: (buf: Buffer) => void;
    const emitted = new Promise<Buffer>((resolve) => {
      resolveEmitted = resolve;
    });
    const outServer = tls.createServer({ key: KEY, cert: CERT }, (socket) => {
      socket.write(buildSpHeader(PROTO_PULL));
      const asm = new MessageAssembler();
      let headerConsumed = false;
      socket.on("data", (chunk: Buffer) => {
        asm.push(chunk);
        if (!headerConsumed) {
          if (asm.length < 8) return;
          asm.consume(8);
          headerConsumed = true;
        }
        const messages = asm.drain();
        if (messages.length > 0) resolveEmitted(messages[0]);
      });
    });

    // Inbound endpoint: acts as the server's PUSH, dispatching one job.
    const inServer = tls.createServer({ key: KEY, cert: CERT }, (socket) => {
      socket.write(buildSpHeader(PROTO_PUSH));
      const dispatch = new Event(
        EventType.JobDispatch,
        { job_id: "j1", payload: {} },
        { driver: "qpu_1" },
      );
      socket.write(frame(Buffer.from(dispatch.toJSON(), "utf8")));
    });

    await new Promise<void>((r) => outServer.listen(0, "127.0.0.1", () => r()));
    await new Promise<void>((r) => inServer.listen(0, "127.0.0.1", () => r()));

    // Handshake endpoint: hands the driver the two NNG ports and the root CA.
    const httpServer = http.createServer((req, res) => {
      if (req.url?.endsWith("/api/op/drivers/connect")) {
        res.setHeader("Content-Type", "application/json");
        res.end(
          JSON.stringify({
            nng_host: "127.0.0.1",
            nng_in_port: tlsPort(inServer),
            nng_out_port: tlsPort(outServer),
          }),
        );
        return;
      }
      if (req.url?.endsWith("/api/pub/root-ca.pem")) {
        res.setHeader("Content-Type", "application/x-pem-file");
        res.end(CERT);
        return;
      }
      res.statusCode = 404;
      res.end();
    });
    await new Promise<void>((r) =>
      httpServer.listen(0, "127.0.0.1", () => r()),
    );
    const httpPort = (httpServer.address() as AddressInfo).port;

    const driver = new EchoDriver({
      qpiAddr: `http://127.0.0.1:${httpPort}`,
      token: "tok_abc",
      name: "qpu_1",
      caFingerprint: FINGERPRINT,
    });

    const running = driver.run();
    const raw = await emitted;
    const result = Event.fromJSON(raw);

    expect(result.type).toBe(EventType.JobResult);
    expect(result.driver).toBe("qpu_1");
    expect(result.payload.job_id).toBe("j1");
    expect(driver.seen.map((e) => e.type)).toContain(EventType.JobDispatch);

    driver.stop();
    await running;
    outServer.close();
    inServer.close();
    httpServer.close();
  }, 15000);

  test("connect surfaces a rejection from the server", async () => {
    const httpServer = http.createServer((_req, res) => {
      res.statusCode = 403;
      res.end("disabled driver");
    });
    await new Promise<void>((r) =>
      httpServer.listen(0, "127.0.0.1", () => r()),
    );
    const httpPort = (httpServer.address() as AddressInfo).port;

    const driver = new EchoDriver({
      qpiAddr: `http://127.0.0.1:${httpPort}`,
      token: "tok",
      name: "qpu_1",
      caFingerprint: FINGERPRINT,
    });
    await expect(driver.run()).rejects.toThrow(/connect rejected \(403\)/);
    httpServer.close();
  });
});
