# Custom domain — turmericstudy.rs.org.pk

**Status: live.** The DNS record resolves to GitHub Pages and the site is
served at **https://turmericstudy.rs.org.pk**.

The `CNAME` file in this repository holds the domain; do not delete it, or
GitHub Pages drops back to the default `github.io` address.

---

## The DNS record in place

| Field | Value |
|---|---|
| Host | `turmericstudy` |
| Resolves to | GitHub Pages (185.199.108–111.153) |
| TTL | 3600 |

Verify at any time:

```bash
nslookup turmericstudy.rs.org.pk 8.8.8.8
```

---

## HTTPS

GitHub issues a Let's Encrypt certificate automatically once the DNS check
passes. In **Settings → Pages**, tick **Enforce HTTPS** as soon as the box
becomes selectable.

This is not optional for this dashboard: the payload is decrypted in the
browser with the WebCrypto API, which browsers expose only in a secure
context. Over plain HTTP the login fails with a message saying so.

---

## Moving the domain later

Change the value in `CNAME`, push, and point the new host at
`jawadahmad-star.github.io.` in DNS. To detach entirely, delete `CNAME`
and clear the *Custom domain* box in Settings → Pages.
