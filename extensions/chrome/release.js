// Only the release tag and expected asset names cross into popup rendering.
export const RELEASE_ORIGIN = "https://api.github.com/*";
const repo = "nguyencanhtrung/asr-studio";
const stable = /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

export function bundleFor(tag) {
  if (!stable.test(tag)) return null;
  return `https://github.com/${repo}/releases/download/${tag}/asr-studio-extension-${tag}.zip`;
}

export function availableBundle(release, installed) {
  const tag = release?.tag_name;
  const target = stable.exec(tag);
  const current = stable.exec(`v${installed}`);
  if (!target || !current || release.draft !== false || release.prerelease !== false) return null;
  const first = [1, 2, 3].find((i) => target[i] !== current[i]);
  if (!first || BigInt(target[first]) <= BigInt(current[first])) return null;
  const url = bundleFor(tag);
  const name = url.split("/").pop();
  const complete = ["", ".sha256", ".metadata.json"].every((suffix) =>
    release.assets?.some((asset) => asset.name === name + suffix && asset.state === "uploaded"
      && asset.size > 0 && asset.browser_download_url === url + suffix));
  return complete ? tag : null;
}

export async function latestBundle(installed) {
  const response = await fetch(`https://api.github.com/repos/${repo}/releases/latest`, {
    credentials: "omit", redirect: "error", referrerPolicy: "no-referrer",
    signal: AbortSignal.timeout(10000)
  });
  if (!response.ok) throw new Error("Release check failed");
  return availableBundle(await response.json(), installed);
}
