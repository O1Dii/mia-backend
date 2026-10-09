// Captures the current web screens in both themes at 390 / 768 / 1440 px and checks that no
// screen scrolls horizontally. Needs the web dev server and the web_poc backend (see design/README
// in handoffs/M33.md). Uses an isolated, disposable browser; no model calls, no real accounts.
//
//   node design/scripts/capture_screenshots.mjs [--url http://127.0.0.1:5133] [--out design/preview/screenshots] [--pages store,dashboard,library,studio] [--seed]
// Default pages are the five committed screens; pass --pages with all seven (adds library, studio) for the scroll check only.
import { createRequire } from "node:module";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const require = createRequire(
  new URL("../../web/package.json", import.meta.url),
);
const { chromium } = require("@playwright/test");

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const base = opt("url", "http://127.0.0.1:5133");
const out = opt(
  "out",
  fileURLToPath(new URL("../preview/screenshots/", import.meta.url)),
);
const pages = opt("pages", "store,dashboard,chat,models,apps").split(",");
const widths = [390, 768, 1440];
const heights = { 390: 844, 768: 1024, 1440: 900 };
const themes = ["dark", "light"];

// Each browser context is its own anonymous workspace, so seed per context.
async function seedWorkspace(page) {
  await page.goto(`${base}/`);
  await page.evaluate(async () => {
    const post = async (path, body) =>
      (
        await fetch("/api/web" + path, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Mia-Web": "1" },
          body: JSON.stringify(body),
        })
      ).json();
    for (const id of ["timer", "clock", "notes", "focus", "alarm", "digest"])
      await post("/plugins/install", { plugin_id: "mia." + id });
    for (const text of [
      "Сделать главное дело дня",
      "Выбрать время для прогулки",
    ])
      await post("/actions", { name: "mia_notes__add", arguments: { text } });
    await post("/dashboard", {
      widgets: [
        { id: "w1", plugin_id: "mia.timer", size: "wide" },
        { id: "w2", plugin_id: "mia.notes", size: "wide" },
        { id: "w3", plugin_id: "mia.clock", size: "wide" },
        { id: "w4", plugin_id: "mia.focus", size: "wide" },
      ],
    });
  });
}

const browser = await chromium.launch();
const failures = [];
try {
  await mkdir(out, { recursive: true });
  for (const theme of themes) {
    for (const width of widths) {
      const ctx = await browser.newContext({
        viewport: { width, height: heights[width] },
        colorScheme: theme,
        reducedMotion: "reduce",
        deviceScaleFactor: 1,
      });
      const page = await ctx.newPage();
      if (args.includes("--seed")) await seedWorkspace(page);
      for (const name of pages) {
        for (let attempt = 1; ; attempt++) {
          try {
            await page.goto(`${base}/#${name}`);
            await page.reload();
            await page.waitForSelector(".main-area", { timeout: 20000 });
            break;
          } catch (e) {
            if (attempt >= 3) throw e;
          }
        }
        await page.evaluate(() => document.fonts.ready);
        await page.waitForTimeout(700);
        const m = await page.evaluate(() => ({
          doc: document.documentElement.scrollWidth,
          client: document.documentElement.clientWidth,
          body: document.body.scrollWidth,
          theme: getComputedStyle(document.documentElement).colorScheme,
        }));
        const ok = m.doc <= m.client && m.body <= m.client;
        console.log(
          `${ok ? "ok  " : "FAIL"} ${name} ${theme} ${width}px scrollWidth=${m.doc}/${m.client} (${m.theme})`,
        );
        if (!ok) failures.push(`${name} ${theme} ${width}`);
        await page.screenshot({ path: `${out}/${name}-${theme}-${width}.png` });
      }
      await ctx.close();
    }
  }
  // The style page itself (file://, no server needed): top of the page, both themes, three widths.
  if (!args.includes("--no-style")) {
    const styleUrl = new URL("../preview/index.html", import.meta.url).href;
    for (const theme of themes) {
      for (const width of widths) {
        const ctx = await browser.newContext({
          viewport: { width, height: heights[width] },
          colorScheme: theme,
          reducedMotion: "reduce",
        });
        const page = await ctx.newPage();
        await page.goto(styleUrl);
        await page.evaluate(() => document.fonts.ready);
        await page.waitForTimeout(500);
        const m = await page.evaluate(() => [
          document.documentElement.scrollWidth,
          document.documentElement.clientWidth,
        ]);
        console.log(
          `${m[0] <= m[1] ? "ok  " : "FAIL"} style ${theme} ${width}px scrollWidth=${m[0]}/${m[1]}`,
        );
        if (m[0] > m[1]) failures.push(`style ${theme} ${width}`);
        await page.screenshot({ path: `${out}/style-${theme}-${width}.png` });
        await ctx.close();
      }
    }
  }
} finally {
  await browser.close();
}
if (failures.length) {
  console.error(`Horizontal scroll on: ${failures.join(", ")}`);
  process.exit(1);
}
