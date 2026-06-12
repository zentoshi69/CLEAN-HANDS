# Sample images

Put local test images here (they are gitignored — never commit user photos).

Good acceptance-test candidates, mirroring the product spec:

1. A selfie holding a wine glass — glove only the holding hand.
2. A meme image with hands — meme stays identical, hands get gloved.
3. An illustration of a hand — gloves match the illustration style.
4. A person holding cash — cash unchanged, fingers gloved.
5. A hoodie mockup with no visible hands — must fail with the no-hands
   message, never invent hands.
6. A group photo — every visible bare hand gloved.

Run the pipeline on any sample without Telegram:

```bash
python -m src.image_pipeline.pipeline samples/selfie.jpg --mode balanced
```

Inspect the outputs written next to the input:

- `*_gloved.png` — final result
- `*_mask.png` — the hand mask that was sent to the provider
- `*_diff.png` — heatmap of what changed (should glow only at the hands)
