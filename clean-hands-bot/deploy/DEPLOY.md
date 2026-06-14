# Deploy — CLEAN glove bot 🧤

**What it does:** DM the bot **any image** → it **instantly deletes your upload** and
replies with the same image, **gloves painted onto every detected hand** (AI inpaint).
No `/meme` command needed — a bare photo is processed immediately.

> Mode: *AI inpaint only.* Images with **no detectable hands are declined** (the AI
> has nothing to glove). Painting real gloves needs an image-edit API (`generic_http`);
> with `IMAGE_PROVIDER=mock` the bot runs end-to-end but won't paint real gloves.

## 1. Bot token
Create a bot with **@BotFather**, copy its token. (For auto-deleting uploads in a
**group**, make the bot an **admin**; in 1-to-1 DMs no admin is needed.)

## 2. venv + dependencies (on the server)
```bash
cd /home/clean/CLEAN-HANDS/clean-hands-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt   # MediaPipe + OpenCV are large; give it a few minutes
```

## 3. Configure `.env`
```bash
cp .env.example .env && nano .env
#  TELEGRAM_BOT_TOKEN=...                          (required)
#  IMAGE_PROVIDER=generic_http                     (for real gloves)
#  IMAGE_PROVIDER_API_KEY=... / IMAGE_PROVIDER_ENDPOINT=...
```

## 4. Install + start the service
```bash
sudo cp deploy/degen-glove.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now degen-glove
sudo systemctl status degen-glove --no-pager
journalctl -u degen-glove -f        # live logs
```

## 5. Test
DM your bot a photo **with hands** → your upload disappears → a gloved photo comes back.

**Updating:** `cd /home/clean/CLEAN-HANDS && sudo -u clean git pull --ff-only && sudo systemctl restart degen-glove`
