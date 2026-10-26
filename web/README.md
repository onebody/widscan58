# Widscan58 web UI

The browser page for the Widscan58 firmware: band scanner, channel plan, lap
timer and browser flashing. Vite + TypeScript, no framework.

```sh
npm ci
npm run dev     # http://localhost:5173, add ?sim to drive it without hardware
npm test
npm run build   # writes dist/
```

## RotorHazard race data and where the page runs

The page reads the current heat from a RotorHazard server at
`http://<rotor-hazard-ip>:5000` and draws the pilot names on the channels. That
only works when the page itself is a **secure context** (https, or http on
localhost) *and* the RotorHazard address passes the same rules:

- **An https page cannot read an http address.** The browser blocks it as mixed
  content before the request leaves the tab; a static host such as GitHub Pages
  cannot proxy it either. There is no front-end workaround.
- **A plain http page on a LAN address is not a secure context**, so Web Serial /
  WebUSB do not exist there and the page cannot talk to the board. `http://localhost`
  is a secure context.

So there are two working setups:

**On the machine the board is plugged into** — no internet needed:

```sh
npm run build
npm run serve            # then open http://localhost:8080/
```

`http://localhost:8080/` is a secure context (board link works) and it can fetch
the LAN RotorHazard server over http (no mixed content). RotorHazard's JSON API
replies with `Access-Control-Allow-Origin: *`, so no proxy or CORS setup is
needed. `serve.mjs` uses only the Node standard library; it can also be copied
next to `dist/` on a machine without `npm install`.

**From a phone, or any device that is not the one running the server** — the
GitHub Pages copy works only if RotorHazard has an **https** address. Put a
tunnel in front of it, for example `cloudflared tunnel --url http://localhost:5000`
or Tailscale Funnel, and enter the `https://…` address in *RotorHazard race
data…*. The field accepts https and the https page reads it directly. The
trade-off: the race API is then reachable from outside the LAN, so use a
tunnel you control.
