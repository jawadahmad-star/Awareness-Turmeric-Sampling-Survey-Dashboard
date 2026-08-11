# Custom domain — turmericstudy.rs.org.pk

The site is currently served from the GitHub Pages default URL:

**https://jawadahmad-star.github.io/Awareness-Turmeric-Sampling-Survey-Dashboard/**

The custom domain is **not** attached yet, deliberately. GitHub redirects the
default URL to the custom domain the moment a `CNAME` file exists in the repo —
so attaching it before DNS resolves would take the site offline for everyone,
including the client, until the DNS record went live.

The domain value is parked in `CNAME.pending`.

---

## Step 1 — DNS record (the domain administrator does this)

Add **one** record on the `rs.org.pk` zone:

| Field | Value |
|---|---|
| Type | `CNAME` |
| Host / Name | `turmericstudy` |
| Points to / Value | `jawadahmad-star.github.io.` |
| TTL | `3600` (1 hour) |
| Proxy / CDN | **DNS only** — must not be proxied |

Notes for the administrator:

- The host is just `turmericstudy`, not the full name — most DNS panels append
  `.rs.org.pk` automatically. If the panel wants the whole name, use
  `turmericstudy.rs.org.pk`.
- The target ends with a dot: `jawadahmad-star.github.io.` — that trailing dot
  matters in BIND-style zone files. Panels that use a plain text box usually do
  not need it.
- Do **not** add an A record as well. A CNAME record must be the only record for
  this hostname.
- TTL 3600 is the recommendation. Anything from 300 to 3600 works; a lower TTL
  only makes the first propagation faster.
- If the zone sits behind Cloudflare, set the record to **DNS only** (grey
  cloud). Orange-cloud proxying breaks GitHub's certificate issuance.

### Verifying the record

```bash
nslookup turmericstudy.rs.org.pk 8.8.8.8
# expect: turmericstudy.rs.org.pk canonical name = jawadahmad-star.github.io
```

Propagation is usually minutes; allow up to 24 hours in the worst case.

---

## Step 2 — attach the domain (after the record resolves)

```bash
git mv CNAME.pending CNAME
git commit -m "Attach custom domain turmericstudy.rs.org.pk"
git push
```

Then in the repository: **Settings → Pages**, confirm *Custom domain* reads
`turmericstudy.rs.org.pk` and the DNS check passes.

## Step 3 — enforce HTTPS

Once the DNS check succeeds, GitHub issues a Let's Encrypt certificate
automatically. This takes a few minutes. When the **Enforce HTTPS** checkbox on
the same page becomes selectable, tick it.

The dashboard *requires* HTTPS: the payload is decrypted in the browser with the
WebCrypto API, which browsers only expose in a secure context. Over plain HTTP
the login will fail with a message saying so.

---

## Rolling back

To detach the domain and return to the github.io URL, rename `CNAME` back to
`CNAME.pending`, push, and clear the *Custom domain* box in Settings → Pages.
