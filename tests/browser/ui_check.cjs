// Headless browser check of the real UI with real FFmpeg conversions of synthetic MP4 files.
// Env: PYTHON (interpreter with deps), PLAYWRIGHT_MODULE, CHROME_PATH, OUT_DIR. Needs ffmpeg on PATH.
"use strict";
const { spawn, execFileSync } = require("child_process");
const path = require("path");
const fs = require("fs");
const net = require("net");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

let PORT;
let BASE;
const OUT = process.env.OUT_DIR || path.join(require("os").tmpdir(), "mak-browser");
const FIX = path.join(OUT, "fixtures");
fs.mkdirSync(path.join(FIX, "second"), { recursive: true });

let assertions = 0;
function check(cond, msg) {
  if (!cond) throw new Error("FAILED: " + msg);
  assertions++;
  console.log("ok -", msg);
}

function makeFixtures() {
  const tone = (freq, file) => execFileSync("ffmpeg", ["-hide_banner", "-loglevel", "error", "-nostdin", "-y",
    "-f", "lavfi", "-i", `sine=frequency=${freq}:duration=2`, "-f", "lavfi", "-i", "color=c=green:s=160x120:r=10",
    "-shortest", "-c:v", "mpeg4", "-c:a", "aac", file]);
  tone(440, path.join(FIX, "song.mp4"));
  tone(880, path.join(FIX, "second", "song.mp4"));
  tone(660, path.join(FIX, "dropped.mp4"));
  fs.writeFileSync(path.join(FIX, "fake.mp4"), "this is not a video\n");
  fs.writeFileSync(path.join(FIX, "notes.txt"), "hello\n");
}

async function waitForServer(server) {
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw new Error("test server exited before becoming ready");
    try { if ((await fetch(BASE + "/")).ok) return; } catch (e) { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("server did not start");
}

(async () => {
  makeFixtures();
  PORT = await new Promise((resolve, reject) => {
    const listener = net.createServer();
    listener.once("error", reject);
    listener.listen(0, "127.0.0.1", () => {const port = listener.address().port; listener.close(() => resolve(port));});
  });
  BASE = `http://127.0.0.1:${PORT}`;
  const runtime = JSON.parse(execFileSync(process.env.PYTHON || "python", ["-I", "-c",
    "import json, site, sys; print(json.dumps({'exe': sys._base_executable, 'paths': site.getsitepackages()}))"]).toString());
  const server = spawn(runtime.exe, ["-I", "-c", "import json, runpy, sys; sys.path[0:0] = json.loads(sys.argv[1]); sys.argv = sys.argv[2:]; runpy.run_path(sys.argv[0], run_name='__main__')",
    JSON.stringify(runtime.paths), path.join(__dirname, "serve.py"), String(PORT), path.join(OUT, "runtime")],
    { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  let serverErrors = "";
  server.stderr.on("data", (data) => { serverErrors = (serverErrors + data.toString()).slice(-65536); });
  server.stdout.on("data", () => {});
  let browser;
  try {
    await waitForServer(server);
    browser = await chromium.launch({ executablePath: process.env.CHROME_PATH, headless: true,
      args: ["--mute-audio", "--no-first-run", "--disable-background-networking", "--disable-sync"] });
    const context = await browser.newContext({ acceptDownloads: true, serviceWorkers: "block" });
    const blocked = [];
    await context.route("**/*", (route) => {
      const url = route.request().url();
      if (url.startsWith(BASE + "/")) return route.continue();
      blocked.push(url);
      return route.abort();
    });
    const page = await context.newPage();
    const errors = [];
    let conversionRequests = 0;
    page.on("request", (request) => {if (request.url() === BASE + "/api/convert") conversionRequests++;});
    // Expected 4xx replies make Chrome log "Failed to load resource"; only script errors count.
    page.on("console", (m) => { if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text()); });
    page.on("pageerror", (e) => errors.push(String(e)));

    await page.goto(BASE + "/");
    check(await page.title() === "MP4 to MP3", "page loads");
    check(!(await page.isVisible("#actions")), "no empty action bar before files");
    await page.screenshot({ path: path.join(OUT, "mp4-empty.png"), fullPage: true });

    // Several files at once: two with the same name, one forged, one wrong type.
    await page.setInputFiles("#files", [path.join(FIX, "song.mp4"), path.join(FIX, "second", "song.mp4"),
      path.join(FIX, "fake.mp4"), path.join(FIX, "notes.txt")]);
    await page.waitForFunction(() => {
      const states = [...document.querySelectorAll("#list li")].map((li) => li.className);
      return states.length === 4 && states.every((c) => c === "ok" || c === "bad");
    }, null, { timeout: 60000 });
    const rows = await page.$$eval("#list li", (lis) => lis.map((li) => [li.className, li.textContent]));
    check(rows[0][0] === "ok" && rows[1][0] === "ok", "two same-name files converted");
    check(rows[2][0] === "bad" && rows[2][1].includes("not an MP4"), "forged MP4 rejected by server");
    check(rows[3][0] === "bad" && rows[3][1].includes("Not an MP4"), "wrong type rejected before upload");
    check(await page.textContent("#zip") === "Download all 2 (ZIP)", "ZIP offered for 2 finished files");

    const [single] = await Promise.all([page.waitForEvent("download"), page.click("#list li:first-child a")]);
    check(single.suggestedFilename() === "song.mp3", "single MP3 download name");
    await single.saveAs(path.join(OUT, "song.mp3"));
    check(fs.readFileSync(path.join(OUT, "song.mp3"))[0] === 0xff, "single download is MP3 data");

    // Drag and drop several files in one batch.
    const b64 = fs.readFileSync(path.join(FIX, "dropped.mp4")).toString("base64");
    await page.evaluate((data) => {
      const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], "dropped.mp4", { type: "video/mp4" }));
      dt.items.add(new File([bytes], "dropped-two.mp4", { type: "video/mp4" }));
      document.getElementById("drop").dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true }));
    }, b64);
    await page.waitForFunction(() => document.querySelectorAll("#list li.ok").length === 4, null, { timeout: 60000 });
    check(true, "drag and drop converts multiple files");
    await page.screenshot({ path: path.join(OUT, "mp4-batch.png"), fullPage: true });

    const [zip] = await Promise.all([page.waitForEvent("download"), page.click("#zip")]);
    check(zip.suggestedFilename() === "mp3-files.zip", "ZIP download name");
    const zipPath = path.join(OUT, "mp3-files.zip");
    await zip.saveAs(zipPath);
    const listing = execFileSync(process.env.PYTHON || "python", ["-I", "-c",
      "import sys, zipfile; print('|'.join(zipfile.ZipFile(sys.argv[1]).namelist()))", zipPath]).toString().trim();
    check(listing === "song.mp3|song (2).mp3|dropped.mp3|dropped-two.mp3", "ZIP entries are unique: " + listing);
    check(await page.textContent("#zip") === "Download all 4 (ZIP)", "ZIP button label is current after a ZIP");

    // Results expire on the server (as after 30 minutes); an old link must not replace the page.
    await page.evaluate(async () => {
      const token = document.querySelector('meta[name="csrf-token"]').content;
      await fetch("/api/clear", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": token }, body: "{}" });
    });
    const pageUrl = page.url();
    const rowCount = await page.locator("#list li").count();
    const [expired] = await Promise.all([page.waitForEvent("download"), page.click("#list li:first-child a")]);
    check((await expired.failure()) !== null, "expired link fails as a download");
    check(page.url() === pageUrl && await page.locator("#list li").count() === rowCount, "expired link keeps the page and list");

    await page.click("#clear");
    await page.waitForFunction(() => document.getElementById("actions").hidden);
    check(await page.locator("#list li").count() === 0 && !(await page.isVisible("#actions")), "clear list");
    for (let i = 0; i < 60 && fs.readdirSync(path.join(OUT, "runtime")).length; i++) await new Promise((resolve) => setTimeout(resolve, 50));
    const left = fs.readdirSync(path.join(OUT, "runtime"));
    check(left.length === 0, "server folder emptied by clear");

    await page.setInputFiles("#files", path.join(FIX, "song.mp4"));
    await page.waitForFunction(() => document.querySelectorAll("#list li.ok").length === 1);
    let releaseClear;
    const clearGate = new Promise((resolve) => { releaseClear = resolve; });
    let clearArrived;
    const clearStarted = new Promise((resolve) => { clearArrived = resolve; });
    let clearContinued;
    const clearReleased = new Promise((resolve) => { clearContinued = resolve; });
    const delayClear = async (route) => { clearArrived(); await clearGate; await route.continue(); clearContinued(); };
    await page.route(BASE + "/api/clear", delayClear);
    try {
      await page.click("#clear");
      await clearStarted;
      const beforeQueued = conversionRequests;
      await page.setInputFiles("#files", path.join(FIX, "dropped.mp4"));
      await page.waitForTimeout(400);
      check(conversionRequests === beforeQueued, "new files wait for the preceding clear acknowledgement");
      check(await page.textContent("#list li .state") === "Waiting...", "queued files stay visible while clearing");
    } finally {
      releaseClear();
      await clearReleased;
      await page.unroute(BASE + "/api/clear", delayClear);
    }
    await page.waitForFunction(() => document.querySelectorAll("#list li.ok").length === 1);
    const freshLink = await page.getAttribute("#list li a", "href");
    check((await context.request.get(BASE + freshLink)).status() === 200, "queued result survives the previous clear");

    await page.route(BASE + "/api/clear", (route) => route.fulfill({ status: 503,
      contentType: "application/json", body: '{"message":"Synthetic clear failure."}' }));
    await page.click("#clear");
    await page.waitForFunction(() => document.getElementById("note").textContent.includes("Synthetic clear failure"));
    const beforeFailedClear = conversionRequests;
    await page.setInputFiles("#files", path.join(FIX, "song.mp4"));
    await page.waitForTimeout(200);
    check(conversionRequests === beforeFailedClear, "failed clear does not release the next upload batch");
    check(await page.isVisible("#clear") && !(await page.isDisabled("#clear")), "failed clear can be retried");
    await page.unroute(BASE + "/api/clear");
    await page.click("#clear");
    await page.waitForFunction(() => document.querySelectorAll("#list li.ok").length === 1);
    check((await page.textContent("#list li .name")) === "song.mp3", "successful retry preserves the queued file");
    await page.route(BASE + "/api/clear", (route) => route.abort("failed"));
    await page.click("#clear");
    await page.waitForFunction(() => document.getElementById("note").textContent.includes("Press Clear list to retry"));
    const beforeDisconnectedClear = conversionRequests;
    await page.setInputFiles("#files", path.join(FIX, "song.mp4"));
    await page.waitForTimeout(200);
    check(conversionRequests === beforeDisconnectedClear, "disconnected clear keeps new uploads queued");
    await page.unroute(BASE + "/api/clear");
    await page.click("#clear");
    await page.waitForFunction(() => document.querySelectorAll("#list li.ok").length === 1);
    check((await page.textContent("#list li .name")) === "song.mp3", "disconnected clear retry preserves the queued file");
    await page.click("#clear");
    await page.waitForFunction(() => document.getElementById("actions").hidden);

    await page.evaluate((data) => {
      const bytes = Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
      const dt = new DataTransfer();
      dt.items.add(new File([bytes], "<img src=x onerror=alert(1)>.mp4", {type: "video/mp4"}));
      document.getElementById("drop").dispatchEvent(new DragEvent("drop", {dataTransfer: dt, bubbles: true, cancelable: true}));
    }, b64);
    await page.waitForFunction(() => document.querySelectorAll("#list li.ok").length === 1);
    check(await page.locator("#list img").count() === 0, "hostile filename stays text");
    await page.setViewportSize({ width: 390, height: 760 });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "mobile result has no horizontal overflow");
    await page.click("#clear");
    await page.evaluate(() => {
      const dt = new DataTransfer();
      for (let i = 0; i < 60; i++) dt.items.add(new File(["synthetic"], "file-" + i + ".txt", {type: "text/plain"}));
      document.getElementById("drop").dispatchEvent(new DragEvent("drop", {dataTransfer: dt, bubbles: true, cancelable: true}));
    });
    check(await page.locator("#list li").count() === 50, "browser batch count is bounded");
    check((await page.textContent("#note")).includes("50 files"), "batch cap is explained");
    await page.click("#clear");
    await page.waitForFunction(() => document.getElementById("actions").hidden);
    check(!(await page.isVisible("#note")), "clear removes prior notices");
    const beforeOversize = conversionRequests;
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File([new Uint8Array(6 * 1024 * 1024)], "oversized.mp4", {type: "video/mp4"}));
      document.getElementById("drop").dispatchEvent(new DragEvent("drop", {dataTransfer: dt, bubbles: true, cancelable: true}));
    });
    check((await page.textContent("#list li .state")).includes("Too large"), "oversize is explained before upload");
    check(conversionRequests === beforeOversize, "oversize bytes are never uploaded");
    await page.click("#clear");
    await page.waitForFunction(() => document.getElementById("actions").hidden);

    await page.setViewportSize({ width: 390, height: 760 });
    await page.screenshot({ path: path.join(OUT, "mp4-mobile.png"), fullPage: true });
    check(errors.length === 0, "no console errors " + JSON.stringify(errors));
    check(blocked.length === 0, "no external requests " + JSON.stringify(blocked));
    check(serverErrors.length === 0, "no server tracebacks or access logs");
    console.log("ALL BROWSER CHECKS PASSED");
    console.log(JSON.stringify({assertions, externalRequests: blocked.length, scriptErrors: errors.length}));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})().catch((e) => { console.error(e); process.exit(1); });
