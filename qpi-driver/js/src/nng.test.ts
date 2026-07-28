import * as net from "node:net";
import * as tls from "node:tls";
import type { AddressInfo } from "node:net";

import {
  DIAL_TIMEOUT_MS,
  PipelineSocket,
  MessageAssembler,
  buildSpHeader,
  parseSpHeader,
  frame,
  PROTO_PUSH,
  PROTO_PULL,
} from "./nng.js";

// A throwaway self-signed cert (CN=localhost, SAN localhost/127.0.0.1) used
// only to exercise the TLS loopback below. Not a secret.
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

describe("SP framing helpers", () => {
  test("buildSpHeader encodes PUSH and PULL protocol numbers", () => {
    expect([...buildSpHeader(PROTO_PUSH)]).toEqual([
      0, 0x53, 0x50, 0, 0, 80, 0, 0,
    ]);
    expect([...buildSpHeader(PROTO_PULL)]).toEqual([
      0, 0x53, 0x50, 0, 0, 81, 0, 0,
    ]);
  });

  test("parseSpHeader returns the peer protocol number", () => {
    expect(parseSpHeader(buildSpHeader(PROTO_PUSH))).toBe(80);
    expect(parseSpHeader(buildSpHeader(PROTO_PULL))).toBe(81);
  });

  test("parseSpHeader rejects a bad magic", () => {
    expect(() =>
      parseSpHeader(Buffer.from([1, 2, 3, 4, 5, 6, 7, 8])),
    ).toThrow();
  });

  test("frame + MessageAssembler round-trip across chunk boundaries", () => {
    const asm = new MessageAssembler();
    const stream = Buffer.concat([
      frame(Buffer.from("hello")),
      frame(Buffer.from("world")),
    ]);

    asm.push(stream.subarray(0, 3));
    expect(asm.drain()).toHaveLength(0); // partial length prefix

    asm.push(stream.subarray(3));
    expect(asm.drain().map((m) => m.toString())).toEqual(["hello", "world"]);
  });
});

describe("PipelineSocket over TLS", () => {
  test("a pull socket receives framed messages from a PUSH peer", async () => {
    const payload = Buffer.from(JSON.stringify({ type: "JobDispatch" }));
    const server = tls.createServer({ key: KEY, cert: CERT }, (socket) => {
      socket.write(buildSpHeader(PROTO_PUSH));
      socket.write(frame(payload));
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", () => resolve()),
    );
    const { port } = server.address() as AddressInfo;

    const sock = new PipelineSocket("pull");
    const received = new Promise<Buffer>((resolve) => sock.onMessage(resolve));
    await sock.dial({
      host: "127.0.0.1",
      port,
      ca: CERT,
      servername: "localhost",
    });

    expect((await received).toString()).toBe(payload.toString());
    sock.close();
    server.close();
  });

  test("a push socket sends framed messages to a PULL peer", async () => {
    let resolveReceived!: (buf: Buffer) => void;
    const received = new Promise<Buffer>((resolve) => {
      resolveReceived = resolve;
    });
    const server = tls.createServer({ key: KEY, cert: CERT }, (socket) => {
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
        if (messages.length > 0) resolveReceived(messages[0]);
      });
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", () => resolve()),
    );
    const { port } = server.address() as AddressInfo;

    const sock = new PipelineSocket("push");
    await sock.dial({
      host: "127.0.0.1",
      port,
      ca: CERT,
      servername: "localhost",
    });
    sock.send(Buffer.from("result"));

    expect((await received).toString()).toBe("result");
    sock.close();
    server.close();
  });

  test("dial rejects when the peer speaks the wrong SP protocol", async () => {
    const server = tls.createServer({ key: KEY, cert: CERT }, (socket) => {
      socket.write(buildSpHeader(PROTO_PULL)); // a PULL socket expects a PUSH peer
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", () => resolve()),
    );
    const { port } = server.address() as AddressInfo;

    const sock = new PipelineSocket("pull");
    await expect(
      sock.dial({ host: "127.0.0.1", port, ca: CERT, servername: "localhost" }),
    ).rejects.toThrow(/protocol mismatch/);
    sock.close();
    server.close();
  });

  test("the default dial deadline matches the Python and Go SDKs' 10s", () => {
    expect(DIAL_TIMEOUT_MS).toBe(10_000);
  });

  // The two ways a peer can go quiet without the connection failing. Neither
  // ends on its own: tls.connect has no timeout beyond the OS TCP timeout, so
  // before the deadline existed both of these hung the driver inside run().
  test("dial fails when the peer accepts the connection and never speaks TLS", async () => {
    // A plain TCP listener: the SYN is answered, the TLS handshake never is —
    // what a firewall that drops packets after connect looks like.
    const server = net.createServer(() => {});
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", () => resolve()),
    );
    const { port } = server.address() as AddressInfo;

    const sock = new PipelineSocket("push");
    await expect(
      sock.dial({
        host: "127.0.0.1",
        port,
        ca: CERT,
        servername: "localhost",
        timeoutMs: 150,
      }),
    ).rejects.toThrow(
      new RegExp(
        `dialling the push channel at 127\\.0\\.0\\.1:${port} timed out after 150ms: the TLS handshake did not complete`,
      ),
    );
    sock.close();
    server.close();
  });

  test("dial fails when the peer completes TLS and never sends its SP header", async () => {
    const server = tls.createServer({ key: KEY, cert: CERT }, () => {});
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", () => resolve()),
    );
    const { port } = server.address() as AddressInfo;

    const sock = new PipelineSocket("pull");
    await expect(
      sock.dial({
        host: "127.0.0.1",
        port,
        ca: CERT,
        servername: "localhost",
        timeoutMs: 150,
      }),
    ).rejects.toThrow(
      new RegExp(
        `dialling the pull channel at 127\\.0\\.0\\.1:${port} timed out after 150ms: the SP handshake did not complete`,
      ),
    );
    sock.close();
    server.close();
  });

  test("dial rejects at once with the socket's own error when nothing is listening", async () => {
    // A connection refused is reported as itself, not waited out and reported
    // as a timeout.
    const sock = new PipelineSocket("push");
    await expect(
      sock.dial({
        host: "127.0.0.1",
        port: 1,
        ca: CERT,
        servername: "localhost",
        timeoutMs: 10_000,
      }),
    ).rejects.toThrow(/ECONNREFUSED/);
    sock.close();
  });

  test("send on a closed socket throws", async () => {
    const server = tls.createServer({ key: KEY, cert: CERT }, (socket) => {
      socket.write(buildSpHeader(PROTO_PULL));
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", () => resolve()),
    );
    const { port } = server.address() as AddressInfo;

    const sock = new PipelineSocket("push");
    await sock.dial({
      host: "127.0.0.1",
      port,
      ca: CERT,
      servername: "localhost",
    });
    sock.close();

    expect(() => sock.send(Buffer.from("late"))).toThrow(/closed socket/);
    server.close();
  });

  test("the deadline does not fire on a connection that idles after dialling", async () => {
    // The deadline bounds the dial only. Sitting idle afterwards is normal for
    // this event-driven transport — there is no polling interval — so a deadline
    // left armed would destroy a socket that is working perfectly.
    let peer!: tls.TLSSocket;
    const server = tls.createServer({ key: KEY, cert: CERT }, (socket) => {
      peer = socket;
      socket.write(buildSpHeader(PROTO_PUSH));
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", () => resolve()),
    );
    const { port } = server.address() as AddressInfo;

    const sock = new PipelineSocket("pull");
    const received = new Promise<Buffer>((resolve) => sock.onMessage(resolve));
    await sock.dial({
      host: "127.0.0.1",
      port,
      ca: CERT,
      servername: "localhost",
      timeoutMs: 150,
    });

    // Idle well past the deadline, then send: the socket must still be there.
    await new Promise<void>((resolve) => setTimeout(resolve, 400));
    peer.write(frame(Buffer.from("late")));
    expect((await received).toString()).toBe("late");

    sock.close();
    server.close();
  });
});
