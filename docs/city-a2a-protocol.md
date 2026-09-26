# Synthetic City/A2A protocol observation

This fork-only prototype observes the structured City/A2A contract inspected at
City commit `7e32887`. It is not official Town accreditation. The Town observer
does its own HTTP, writes a native Path bundle, and uses the native Ed25519
attestation and receipt pipeline. Its pure evaluator replays retained bytes.

## Scope and fixed recipe

- Profile: `city-a2a-protocol@0.1`
- Capability: `city-a2a-structured-task`
- Evaluator: `city-a2a-protocol-evaluator@0.1`
- Result version: `path-city-a2a-protocol-0.1`
- Profile fingerprint:
  `sha256:e6a1cc01584de3547a76ddc3b2bbac6d366258bc8603a26a1dad2ebd5c5212cc`

The required stages are `pinned_card`, `structured_send`, `acceptance_task`,
`exact_retry`, and `terminal_task`. Later stages remain untested after a broken
boundary. Transport/poll budget exhaustion is incomplete; protocol mismatch is
failed; internal observer malfunction is error. Cancellation propagates without
writing or signing a bundle.

The observed claim is protocol shape and one retry only: not EIP-712 signature
validity, Ethereum authorization, ownership, independent operators, truthful
venues, answer quality, or global exactly-once execution. The signed receipt
states these limitations. Replay is an evaluation of the observer's record, not
an independent rerun or proof that the observer reported truthfully.

## Inputs and run

Use an owned synthetic service and a separately pinned card file. Prepare a full
already-signed JSON-RPC `message/send` document using City's existing caller:
`params.message.parts[0].data` has type `org.nandacity.city-request`, version
`0.1`, and the signed `envelope`. The observer neither signs that request nor
loads a caller wallet. It sends the supplied file bytes unchanged on both send
and retry, including the same RPC ID; each response must correlate to that ID.
It generates separately correlated `tasks/get` requests for the returned task.

Supply exact URLs, including explicit ports and paths, using literal
`http://127.0.0.1:PORT/path` or `http://[::1]:PORT/path`. The card origin may differ
from the invocation origin. Hostnames, credentials, queries, fragments, escapes,
and dot segments are refused. The card cannot expand the allowlist: its `url`
must equal the independently supplied invocation URL. No redirects or inherited
proxy settings are used.

Use a disposable observer key directory, separate from all user/caller keys:

```sh
observer_home=$(mktemp -d)
env -i PATH="/absolute/town/.venv/bin:/usr/bin:/bin" \
  NANDATOWN_HOME="$observer_home/town-home" \
  /absolute/town/.venv/bin/python -m nandatown.city_path \
  --subject-url http://127.0.0.1:9001/ \
  --card-url http://127.0.0.1:9002/cards/7.json \
  --pinned-card /absolute/synthetic/card.json \
  --request /absolute/synthetic/signed-request-rpc.json \
  --out-dir "$observer_home/bundles" \
  --observer-key-dir "$observer_home/observer-keys" \
  --observer-name synthetic-town-observer
```

All flags are required. Output is one JSON object naming the bundle, receipt and
verdict. Exit codes: 0 passed; 1 failed/incomplete/error; 2 invalid caller input.
Retain the disposable observer public identity if a later local admission policy
explicitly selects it; do not publish private keys. This command is separate from
the normal quote runner, which refuses this profile.

## Evidence and limits

The fixed fingerprint does not vary per run. A manifest-bound `city_run` event
retains the exact pinned card and synthetic request as Base64. Each `city_http`
event retains the request and bounded raw response body, method, exact URL,
HTTP status, monotonic start/end offsets, and transport outcome. Recording
attempted request bytes is not a delivery claim.
When a budget prevents an HTTP attempt, its event retains no request body.
Parsing is strict UTF-8/JSON: duplicate keys, nonfinite numbers, lone surrogates and JSON
containers at depth 32 or beyond are refused. Untrusted bodies stay Base64 in
events, so malformed Unicode or oversized echoes cannot corrupt JSONL writing.

The recipe caps observation at 30 seconds, each complete response (including
stream termination) at 5 seconds, card bodies at 64 KiB, other individual bodies
at 1 MiB, aggregate recorded HTTP request/response body bytes at 4 MiB, and polling
at 20 attempts. HTTP headers and local evidence encoding overhead are not body
bytes. A limited response retains its bounded prefix and cannot pass. Run
metadata records the start before HTTP, the inspected Town base commit, exact
observer/profile/dispatch source hashes, interpreter and evaluator versions.

Town's receipt card digest is SHA-256 of its canonical parsed card, not City's
Keccak of exact card bytes. The run also retains exact bytes and their SHA-256.
The later City bridge must independently bind those bytes, subject, current
owner/profile basis, city/task, selected observer and freshness policy.

## Native verification and tests

```python
from nandatown.bundle import verify_bundle
from nandatown.receipt import verify_receipt

assert verify_bundle(bundle_directory) == []
assert verify_receipt(receipt_path, bundle_directory) == []
```

Both checks matter. Integrity or replay failure prevents normal receipt creation;
a supplied pass flag never replaces replay. `tests/test_city_path.py` uses owned
loopback sockets, literal synthetic wire shapes and disposable observer keys,
including real slow-body, missing-stream-EOF and cumulative deadline tests.
The synthetic envelope vectors are structurally signed, not valid Ethereum
authorization evidence. A real owned City-service run and independent City
admission checks remain separate acceptance gates.
