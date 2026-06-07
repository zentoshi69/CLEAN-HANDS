# 🤖 @BotFather — the order sheet

Everything @BotFather needs, in order. Replace `app.cleanhands.fun` with your real
domain. (The Bot-API-settable parts — names, commands, admin rights, menu button —
are automated by `staking-api/../configure.py`; this sheet covers the rest, which
only @BotFather can do.)

## 1. Privacy mode → Disable (so bots can read group messages)

For **each** of Guardian, Scanner, Community:

```
/setprivacy → pick the bot → Disable
```

Then remove & re-add the bot to the group once (so the change takes effect).

## 2. Register the Mini App on the Community bot

```
/newapp → pick the Community bot
  Title:        $CLEAN
  Description:  Stake, trade, boost, leaderboard, referrals
  Photo/icon:   upload the glove (bots/assets/glove.png)
  Web App URL:  https://app.cleanhands.fun/
  Short name:   app
```

This makes the deep link `https://t.me/<CommunityBot>/app` open the Mini App, and
referral links `…/app?startapp=<wallet>` work.

> Already created it earlier? Use **Bot Settings → Configure Mini App** and just
> set the **Web App URL** to `https://app.cleanhands.fun/`.

## 3. Group settings (allow groups + inline as desired)

```
/mybots → Community bot → Bot Settings → Allow Groups? → Enabled
```

## 4. Run the automated config (everything else)

On the server, with `.env` filled in:

```
cd bots && python configure.py
```

Sets each bot's name/description, the slash-command menus, default group admin
rights (Guardian: delete+ban+restrict, Scanner: delete), and the Community bot's
**menu button → the Mini App**.

## 5. In the group

```
/setup           # Guardian locks down + pins rules + invite link
/refreshadmins   # arms the impersonation guard
```

## Recap of URLs

- Mini App URL (BotFather): `https://app.cleanhands.fun/`
- Deep link: `https://t.me/<CommunityBot>/app`
- API base (site + app): `https://app.cleanhands.fun/api`
