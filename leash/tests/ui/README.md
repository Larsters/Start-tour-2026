# Headless UI check

Runs `web/index.html` in jsdom against a recorded engine event stream (`fixture.json`)
and asserts the wheel, history, replay and node-click paths work without JS errors.

```bash
cd tests/ui && npm i && npm test
```

Regenerate `fixture.json` by recording `/events`, `/cards`, `/customers`, `/health` from an offline session.
