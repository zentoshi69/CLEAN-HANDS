# 🔗 CLEAN site SDK — keep your website in sync with the Mini App

`clean-staking.js` is a tiny, dependency-free browser client for the CLEAN
staking API. Drop it on your website and it calls the **exact same endpoints** the
Telegram Mini App uses — one backend, one source of truth, so staked amounts,
APR, burns, leaderboard and rewards are **always identical** across web + Telegram.

## Use

```html
<script src="/clean-staking.js"></script>
<script>
  const clean = new CleanStaking("https://app.cleanhands.fun", { persist: true });
  await window.solana.connect(); // Phantom
  const me = await clean.login(window.solana); // wallet-signature login
  await clean.stake();
  await clean.claim();
  const board = await clean.leaderboard();
</script>
```

`new CleanStaking(apiBase, { persist })` · `login(provider,{ref})` ·
`profile()` · `stake()` · `unstake()` · `claim()` · `burn(txSig)` ·
`leaderboard()` · `referrals()` · `price()` · `economics()` · `logout()`.

`provider` is any Solana wallet exposing `publicKey` + `signMessage`
(Phantom/Solflare/Backpack extensions, or a wallet-standard adapter). On the web,
extensions make this simpler than the Mini App's deeplink flow — same backend
either way.

## One server-side requirement

The website is a **different origin** from the API, so add your site's origin to
the API's CORS allow-list:

```
STAKE_CORS_ORIGINS=https://cleanhands.fun,https://www.cleanhands.fun
```

## Why this guarantees sync

There is no second database or second set of rules. The website and the Mini App
are both thin clients of the staking API; identity is the wallet (verified by
signature). A user who stakes on the site sees the same state in Telegram and
vice-versa, instantly. `example.html` is a working reference page.

> Security: the SDK holds only a short-lived session token (never a secret). All
> validation and money logic live on the server.
