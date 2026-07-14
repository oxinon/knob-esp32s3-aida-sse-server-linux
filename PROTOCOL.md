# The AIDA64 RemoteSensor protocol (reverse-engineered)

This document describes how the Waveshare **ESP32-S3-Knob-Touch-LCD-1.8**
(firmware `WX-ESP32S3-KNOB_V1.2.bin`, as of 2026) actually communicates
with AIDA64. Neither Waveshare nor AIDA64 publicly document this wire
format — the information below was obtained through firmware analysis
(`strings`) and Wireshark captures of a real, working AIDA64 instance.

## TL;DR

- The Knob periodically makes a `GET /sse` request to the "PC Monitor" IP
  configured in its web UI.
- The server responds **once per request** (no ongoing streaming despite
  the SSE content type) with HTTP headers and a single-line `data:` line.
- **The response has to arrive within a few hundred milliseconds.** The
  Knob apparently gives up fairly quickly and won't process a late (but
  otherwise correct) response anymore. See
  [Pitfall 3](#pitfall-3-response-time).

## The request

```
GET /sse HTTP/1.1
Host: <ip>
Connection: close

```
(with `\r\n` line endings, entirely normal HTTP/1.1)

Observation: the firmware sends the request in **two separate TCP
segments** (first about 17 bytes, then the rest) — presumably two
consecutive `send()` calls in the firmware code. That's transparent to a
server using the standard socket API, but it stood out in the Wireshark
capture.

## The response

Byte-exact capture of a real AIDA64 response:

```
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
Access-Control-Allow-Origin: *
Access-Control-Expose-Headers: *
Access-Control-Allow-Credentials: true

data: Page0|{|}Simple1|CPU usage 17^{|}Simple2|CPU freq 1600^{|}Simple3|CPU temp 57^{|}Simple4|CPU fan 6578^{|}Simple5|{|}Simple6|GPU freq 100^{|}Simple7|GPU temp TRIAL^{|}Simple8|GPU fan 0^{|}
```

### Pitfall 1: line endings are `\n`, not `\r\n`

This is the crux of the whole problem. Technically HTTP/1.1 requires `\r\n`
as the line ending. **AIDA64's built-in web server doesn't stick to that
and sends only `\n`.** Browsers and `curl` are lenient and parse it
correctly anyway — the Knob firmware (presumably) isn't: it likely scans
strictly for the byte sequence `\n\n` to detect the end of headers.
`\r\n\r\n` doesn't contain `\n\n` as a substring, so standards-compliant
`\r\n` responses (like Python's `http.server` produces automatically)
never yield a detected end of headers.

**Consequence:** build headers by hand as raw bytes with `\n`, don't use
the standard library's `send_header()`/`end_headers()` helpers.

### Pitfall 2: Simple IDs are positional slots, not sensor IDs

You might expect the ID (`SCPUUTI`, `TCPUPKG`, ...) from AIDA64's
"Complete Sensor Value List" to be transmitted. **That's not the case.**
Instead, `SimpleN` is transmitted, where `N` is simply the position (1-8)
of the entry in the imported `.rslcd` layout file. The firmware therefore
doesn't know about AIDA64 sensor IDs at all (e.g. the string `SCPUUTI`
doesn't appear anywhere in the compiled binary) — it matches purely
positionally against its own hardcoded labels (`CPU usage`, `CPU freq`,
`CPU temp`, `CPU fan`, `GPU usage`, `GPU freq`, `GPU temp`, `GPU fan` — in
exactly this order).

Format per entry: `SimpleN|<Label> <Value>^`. Multiple entries are joined
with `{|}`, and the whole payload starts with an empty `Page0|` prefix.

The `^` at the end of each value is **mandatory** and comes from the
`.rslcd`'s `Show unit` field (literally set to `^` there) — it apparently
serves as a terminator character, not an actual unit.

### Pitfall 3: response time

The real show-stopper, once the format and line endings were already
correct. Per Wireshark timing comparison:

| Server | Time from "request fully received" to "response sent" |
|---|---|
| Real AIDA64 | ~56 ms |
| Our first Python attempt (with `intel_gpu_top`/`nvidia-smi` calls at a 2s timeout) | ~2200 ms |

At delays above ~2 seconds, the response arrived cleanly and completely on
the wire (and was even ACKed by the Knob) — but was never shown on the
display. Hypothesis: the firmware internally has a short read-timeout and
gives up on the socket at the application level before late data gets
processed, even if it's technically sitting in the receive buffer at the
TCP level.

**Consequence:** sensor queries need to stay in the low single-digit
millisecond range. In particular, `intel_gpu_top` (often needs
root/`CAP_PERFMON` and can hang for a long time without those privileges)
should be avoided or given a very short timeout.

## Order / positions (from the bundled `.rslcd`)

| Slot | Label | ITMY (pixel position) |
|---|---|---|
| Simple1 | CPU usage | 0 |
| Simple2 | CPU freq | 20 |
| Simple3 | CPU temp | 40 |
| Simple4 | CPU fan | 60 |
| Simple5 | GPU usage | 100 |
| Simple6 | GPU freq | 120 |
| Simple7 | GPU temp | 140 |
| Simple8 | GPU fan | 160 |

## Open questions / not fully resolved

- Whether the firmware really does scan strictly for `\n\n`, or whether
  there's some other reason for the CRLF sensitivity, hasn't been
  verified via disassembly (no Xtensa toolchain on hand) — only confirmed
  empirically that switching from `\r\n` to `\n` fixed the problem.
- Why the firmware sometimes half-closes its request stream via FIN
  immediately and sometimes doesn't (also observed with real AIDA64,
  about 1 in 6 connection attempts there got no server response at all)
  is unresolved. Likely a quirk of the ESP32 network stack (lwIP) under
  WiFi load.
- The exact timeout value beyond which the firmware discards a response
  wasn't precisely pinned down (we only know: 56ms works, 2200ms
  doesn't).
