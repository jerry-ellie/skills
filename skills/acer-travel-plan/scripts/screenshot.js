const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const { chromium } = require('playwright');

// Prefer the system's own installed Chrome over Playwright's bundled Chromium
// when one is present (endpoint security tends to flag the bundled download).
async function launchBrowser() {
  try {
    return await chromium.launch({ channel: 'chrome' });
  } catch {
    return await chromium.launch();
  }
}

(async () => {
  const target = process.argv[2];
  const screenshotPath = process.argv[3];

  if (!target || !screenshotPath) {
    console.error('Usage: node screenshot.js <html-path-or-url> <screenshot-path>');
    process.exit(1);
  }

  // A plain filesystem path used to be rejected (it had to be file://...);
  // that rule tripped small models often enough that it is now just handled.
  const url = /^[a-z][a-z0-9+.-]*:\/\//i.test(target) ? target : pathToFileURL(path.resolve(target)).href;

  const browser = await launchBrowser();
  const context = await browser.newContext({
    viewport: { width: 1280, height: 800 }
  });
  const page = await context.newPage();

  try {
    // Long trips embed many remote photos; 'networkidle' rarely settles in time,
    // so wait for the DOM, then give images a bounded window to finish.
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.evaluate(() => {
      document.querySelectorAll('img[loading="lazy"]').forEach((img) => { img.loading = 'eager'; });
    });
    // The PNG is a preview: a few photos still loading are not worth holding fill.py (and the
    // model's reply) for long, so the wait is short.
    await page
      .waitForFunction(() => Array.from(document.images).every((img) => img.complete), null, { timeout: 12000 })
      .catch(() => console.error('Some images did not finish loading; capturing anyway'));

    await page.screenshot({
      path: screenshotPath,
      fullPage: true
    });

    const size = fs.existsSync(screenshotPath) ? fs.statSync(screenshotPath).size : 0;
    if (!size) throw new Error('PNG was not written or is empty');
    console.log(`Screenshot saved as ${screenshotPath} (${size} bytes)`);
  } catch (error) {
    console.error('Error taking screenshot:', error);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
