import assert from "node:assert/strict";
import test from "node:test";
import {availableBundle, bundleFor} from "../release.js";

const tag = "v0.10.0";
const name = "asr-studio-extension-v0.10.0.zip";
const base = "https://github.com/nguyencanhtrung/asr-studio/releases/download/v0.10.0/";
// Asset fields and the incomplete starter state: https://docs.github.com/en/rest/releases/assets
const assets = ["", ".sha256", ".metadata.json"].map((suffix) => ({
  name: name + suffix, size: 100, state: "uploaded", browser_download_url: base + name + suffix
}));
const release = {tag_name: tag, draft: false, prerelease: false, assets};

test("a newer stable bundle requires all three published assets of that release", () => {
  assert.equal(availableBundle(release, "0.9.8"), tag);
  for (const current of ["0.10.0", "0.11.0"]) assert.equal(availableBundle(release, current), null);
  for (const change of [{draft: true}, {prerelease: true}, {assets: assets.slice(0, 2)},
    {assets: assets.map((asset) => ({...asset, state: "starter"}))},
    {assets: assets.map((asset) => ({...asset, browser_download_url: "https://example.org/file"}))}]) {
    assert.equal(availableBundle({...release, ...change}, "0.9.8"), null);
  }
  assert.equal(bundleFor("v0.10.0/../bad"), null);
});
