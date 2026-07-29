import { fingerprintOf, verifyFingerprint, pemToDer } from "./ca.js";

// The throwaway cert from nng.test.ts and its known DER SHA-256.
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
const FINGERPRINT =
  "faf36b68aef4fd1e4300c9c55f4eaf239c3e2395b58340f83d02032f65176147";

describe("CA pinning", () => {
  test("fingerprintOf matches the DER SHA-256 openssl reports", () => {
    expect(fingerprintOf(CERT)).toBe(FINGERPRINT);
  });

  test("verifyFingerprint accepts the pinned value (case-insensitive)", () => {
    expect(() =>
      verifyFingerprint(CERT, FINGERPRINT.toUpperCase()),
    ).not.toThrow();
  });

  test("verifyFingerprint rejects a mismatch", () => {
    expect(() => verifyFingerprint(CERT, "deadbeef")).toThrow(/does not match/);
  });

  test("verifyFingerprint refuses to run without a pinned fingerprint", () => {
    // It used to return quietly here, which made an unpinned connection reachable
    // by leaving an argument out — the kind of opt-out a copy-pasted command hits
    // by accident (RFC 0003 §10). There is now no way to skip the check.
    expect(() => verifyFingerprint(CERT, "")).toThrow(
      /a CA fingerprint is required/,
    );
  });

  test("pemToDer rejects non-PEM input", () => {
    expect(() => pemToDer("not a cert")).toThrow();
  });
});
