# Prompt for Claude Code — apply the Musaed brand in Frappe

Copy this folder (`musaed-brand/`) into the root of your Frappe app repository, then paste the prompt below into Claude Code. Replace `<app_name>` with your app's module name.

---

```
You are working in the Frappe app "<app_name>" (the Musaed AI chat assistant for ERPNext).
The folder ./musaed-brand/ contains the approved brand kit. Read ./musaed-brand/README.md first;
it is the source of truth for colours, fonts, logo rules and robot states. Do not invent new
colours, shapes or icons — use only what is in the kit.

Do the following:

1. Assets
   - Copy musaed-brand/assets, tokens/tokens.css and motion/motion.css into
     <app_name>/public/musaed/ (keep the sub-folder structure).
   - Do not modify the SVG files; they are final.

2. Global styles
   - In hooks.py add to app_include_css (and web_include_css if the app has web pages):
       "/assets/<app_name>/musaed/tokens/tokens.css",
       "/assets/<app_name>/musaed/motion/motion.css"
   - Load the fonts once: Readex Pro (600, 700) and IBM Plex Sans Arabic (400, 500, 600)
     from Google Fonts, or self-host them under public/musaed/fonts if the deployment is offline.
   - Every colour in new CSS must come from the --ms-* variables in tokens.css. No hex values.
   - Respect dark mode: tokens.css already switches on prefers-color-scheme and on
     [data-theme="dark"] — map Frappe's desk theme attribute to data-theme if needed.

3. Branding in Frappe
   - App icon / logo: use assets/svg/icon/icon-primary.svg for app_logo_url in hooks.py
     and for the app tile on the /app home.
   - Favicon: assets/favicon/favicon.ico (+ apple-touch-icon.png, android-chrome-*.png)
     for any web pages the app serves.
   - Where the product name appears next to the icon, use
     assets/svg/logo/lockup-horizontal.svg (light) or lockup-horizontal-reversed.svg (dark).
     Never retype the wordmark in another font.

4. Chat widget
   - Port components/chat-widget-demo.html into the app's existing chat UI
     (or create it as a desk-wide floating widget loaded via app_include_js if none exists).
     Keep its structure and class names (.ms-launcher, .ms-panel, .ms-header, .ms-msg--user,
     .ms-msg--agent, .ms-composer …); remove the demo state buttons.
   - The header robot must be the inline SVG from components/robot.svg.html (not <img>),
     so motion.css can animate it. Agent message avatars use assets/svg/icon/avatar-round.svg.
   - Drive the header robot's data-state from the real request lifecycle:
       panel opened            → "welcome" (for ~1.5 s, then "idle")
       user typing             → "listening"
       request sent            → "processing"
       reply streaming         → "thinking"
       reply complete          → "done" (for ~2 s, then "idle")
       2 minutes no activity   → "sleep"
   - The widget is RTL (dir="rtl"); the launcher sits bottom-left. Keep it keyboard accessible:
     real <button>s, aria-label on icon-only buttons, aria-expanded on the launcher,
     aria-live="polite" on the message log.

5. Verify
   - Build assets (bench build --app <app_name>) and open the desk: check the launcher,
     open/close, each robot state, light and dark mode, and the favicon.
   - Report which files you changed.
```

---

### Notes for the developer
- The kit uses placeholder content like `[الكمية]` in the demo — replace with real data from the assistant's responses.
- The wordmark in `assets/svg/logo/*` is outlined (vector paths), so it renders correctly even where Readex Pro is not loaded.
- PNG exports in `assets/png/` are for places that cannot take SVG (app stores, email, presentations).
