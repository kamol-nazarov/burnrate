# Optional Tailscale Serve

Tailscale is **not** installed, started, or configured by BURNRATE. The dashboard binds `127.0.0.1:17331` and is meant to stay there.

If you already run Tailscale and want HTTPS access from other devices on **your** tailnet, use **Serve**, not Funnel.

## Serve to localhost

Point Serve at the local dashboard only:

```powershell
tailscale serve --bg --https=443 http://127.0.0.1:17331
```

Then open:

```text
https://<magicdns-name>/
```

Replace `<magicdns-name>` with the MagicDNS name of **this** machine (the name Tailscale shows for the node). BURNRATE docs never ship a real hostname.

Stop Serve with `tailscale serve reset` (or the equivalent in your Tailscale version) when you no longer want tailnet access. That does not stop `burnrate serve`.

## Funnel is out of scope

[Tailscale Funnel](https://tailscale.com/kb/1223/funnel) publishes a path to the public Internet. BURNRATE does not enable Funnel, does not document it as a feature, and does not treat a Funnel URL as a product URL.

If you deliberately enable Funnel outside BURNRATE, you are exposing a localhost usage dashboard — including model names, token counts, and any subscriptions you entered — to the open Internet. Do not do that unless you have a separate access-control plan. Provider credentials are not returned by the API, but the usage metadata is still yours.

## What BURNRATE will not do

- Bind `0.0.0.0` or a public interface
- Print a tailnet URL on startup
- Health-check MagicDNS or Funnel
- Split ports for other local apps

The only supported remote shape is: your Serve config → `http://127.0.0.1:17331`.
